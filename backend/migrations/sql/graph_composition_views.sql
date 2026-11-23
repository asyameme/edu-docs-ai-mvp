-- Прежние имена остаются совместимым представлением состава, включая старые building.
CREATE VIEW graph_entities AS
 SELECT c.program_id,c.graph_state_id,r.entity_id,r.revision_id,(x.position-1)::integer AS position
 FROM graph_state_contents c CROSS JOIN LATERAL unnest(c.entity_revision_ids) WITH ORDINALITY x(id,position)
 JOIN entity_revisions r ON r.revision_id=x.id
 UNION ALL SELECT g.* FROM graph_entities_legacy g
 WHERE NOT EXISTS(SELECT 1 FROM graph_state_contents c WHERE c.graph_state_id=g.graph_state_id);
CREATE VIEW graph_relations AS
 SELECT c.program_id,c.graph_state_id,r.relation_id,r.revision_id,r.source_entity_id,r.target_entity_id,
   (x.position-1)::integer AS position
 FROM graph_state_contents c CROSS JOIN LATERAL unnest(c.relation_revision_ids) WITH ORDINALITY x(id,position)
 JOIN relation_revisions r ON r.revision_id=x.id
 UNION ALL SELECT g.* FROM graph_relations_legacy g
 WHERE NOT EXISTS(SELECT 1 FROM graph_state_contents c WHERE c.graph_state_id=g.graph_state_id);
CREATE VIEW graph_documents AS
 SELECT c.program_id,c.graph_state_id,d.document_id,d.parsed_artifact_id,d.parsed_artifact_kind,d.member_name,
   (x.position-1)::integer AS position
 FROM graph_state_contents c CROSS JOIN LATERAL unnest(c.document_selection_ids) WITH ORDINALITY x(id,position)
 JOIN graph_document_selections d ON d.id=x.id
 UNION ALL SELECT g.* FROM graph_documents_legacy g
 WHERE NOT EXISTS(SELECT 1 FROM graph_state_contents c WHERE c.graph_state_id=g.graph_state_id);
ALTER VIEW graph_documents ALTER COLUMN parsed_artifact_kind SET DEFAULT 'parsed_json';
CREATE VIEW graph_sources AS
 SELECT c.program_id,c.graph_state_id,r.id AS source_ref_id,r.document_id,(x.position-1)::integer AS position
 FROM graph_state_contents c CROSS JOIN LATERAL unnest(c.source_ref_ids) WITH ORDINALITY x(id,position)
 JOIN source_refs r ON r.id=x.id
 UNION ALL SELECT g.* FROM graph_sources_legacy g
 WHERE NOT EXISTS(SELECT 1 FROM graph_state_contents c WHERE c.graph_state_id=g.graph_state_id);
CREATE FUNCTION graph_evidence_members(state_id uuid)
RETURNS TABLE(source_link_id uuid,entity_id uuid,relation_id uuid,source_ref_id uuid,"position" integer,public_id uuid)
LANGUAGE sql STABLE SET search_path=pg_catalog,public,pg_temp AS $$
  WITH selected AS MATERIALIZED (SELECT * FROM graph_state_contents WHERE graph_state_id=state_id),
  facts AS MATERIALIZED (
    SELECT l.id,l.entity_id,l.relation_id,l.source_ref_id,p.public_id
    FROM selected c CROSS JOIN LATERAL unnest(c.evidence_set_ids) s(id)
    JOIN revision_evidence_sets e ON e.id=s.id
    CROSS JOIN LATERAL unnest(e.source_link_ids,e.public_ids) p(fact_id,public_id)
    JOIN source_links l ON l.id=p.fact_id
  ), ordering AS MATERIALIZED (
    SELECT x.public_id,(x.position-1)::integer AS position FROM selected c
    JOIN graph_evidence_orders o ON o.id=c.evidence_order_id
    CROSS JOIN LATERAL unnest(o.public_ids) WITH ORDINALITY x(public_id,position)
  )
  SELECT f.id,f.entity_id,f.relation_id,f.source_ref_id,o.position,f.public_id
  FROM facts f JOIN ordering o ON o.public_id=f.public_id
$$;
CREATE VIEW graph_source_links AS
 SELECT c.program_id,c.graph_state_id,m.source_link_id,m.entity_id,m.relation_id,m.source_ref_id,
   m.position,m.public_id
 FROM graph_state_contents c CROSS JOIN LATERAL graph_evidence_members(c.graph_state_id) m
 UNION ALL SELECT g.* FROM graph_source_links_legacy g
 WHERE NOT EXISTS(SELECT 1 FROM graph_state_contents c WHERE c.graph_state_id=g.graph_state_id);

CREATE FUNCTION graph_legacy_view_write() RETURNS trigger LANGUAGE plpgsql
SET search_path=pg_catalog,public,pg_temp AS $$
DECLARE sid uuid; pid uuid; key_column text; assignments text;
BEGIN
  IF TG_OP='DELETE' THEN sid:=OLD.graph_state_id; pid:=OLD.program_id;
  ELSE sid:=NEW.graph_state_id; pid:=NEW.program_id; END IF;
  PERFORM graph_lock_program(pid);
  IF NOT EXISTS(SELECT 1 FROM graph_states WHERE id=sid AND program_id=pid AND status='building')
  THEN RAISE EXCEPTION 'Состав состояния неизменяем' USING ERRCODE='23514'; END IF;
  IF EXISTS(SELECT 1 FROM graph_state_contents WHERE graph_state_id=sid)
  THEN RAISE EXCEPTION 'Состав состояния неизменяем' USING ERRCODE='23514'; END IF;
  IF TG_OP='UPDATE' AND (NEW.program_id<>OLD.program_id OR NEW.graph_state_id<>OLD.graph_state_id)
  THEN RAISE EXCEPTION 'Перенос состава запрещён' USING ERRCODE='23514'; END IF;
  key_column:=CASE TG_TABLE_NAME WHEN 'graph_entities' THEN 'entity_id'
    WHEN 'graph_relations' THEN 'relation_id' WHEN 'graph_documents' THEN 'document_id'
    WHEN 'graph_sources' THEN 'source_ref_id' ELSE 'source_link_id' END;
  IF TG_OP='INSERT' THEN
    EXECUTE format('INSERT INTO %I SELECT ($1).*',TG_TABLE_NAME||'_legacy') USING NEW;
  ELSIF TG_OP='UPDATE' THEN
    SELECT string_agg(format('%I=($1).%I',attname,attname),',') INTO assignments
      FROM pg_attribute WHERE attrelid=TG_RELID AND attnum>0 AND NOT attisdropped;
    EXECUTE format('UPDATE %I SET %s WHERE graph_state_id=($2).graph_state_id AND %I=($2).%I',
      TG_TABLE_NAME||'_legacy',assignments,key_column,key_column) USING NEW,OLD;
  ELSE
    EXECUTE format('DELETE FROM %I WHERE graph_state_id=($1).graph_state_id AND %I=($1).%I',
      TG_TABLE_NAME||'_legacy',key_column,key_column) USING OLD;
    RETURN OLD;
  END IF;
  RETURN NEW;
END $$;

DO $$
DECLARE t text; role_name text; privilege_name text; grant_row record;
BEGIN
  FOREACH t IN ARRAY ARRAY['graph_entities','graph_relations','graph_documents','graph_sources','graph_source_links'] LOOP
    EXECUTE format('CREATE TRIGGER legacy_write INSTEAD OF INSERT OR UPDATE OR DELETE ON %I '
      'FOR EACH ROW EXECUTE FUNCTION graph_legacy_view_write()',t);
    FOR grant_row IN SELECT a.grantee,a.privilege_type FROM pg_class c
      CROSS JOIN LATERAL aclexplode(c.relacl) a WHERE c.oid=(t||'_legacy')::regclass LOOP
      role_name:=CASE WHEN grant_row.grantee=0 THEN 'PUBLIC' ELSE quote_ident(pg_get_userbyid(grant_row.grantee)) END;
      privilege_name:=grant_row.privilege_type;
      IF privilege_name IN ('SELECT','INSERT','UPDATE','DELETE') THEN
        EXECUTE format('GRANT %s ON %I TO %s',privilege_name,t,role_name);
      END IF;
    END LOOP;
  END LOOP;
  FOR grant_row IN SELECT DISTINCT a.grantee FROM pg_class c
    CROSS JOIN LATERAL aclexplode(c.relacl) a WHERE c.oid='graph_states'::regclass
      AND a.privilege_type='INSERT' LOOP
    role_name:=CASE WHEN grant_row.grantee=0 THEN 'PUBLIC' ELSE quote_ident(pg_get_userbyid(grant_row.grantee)) END;
    EXECUTE format('GRANT SELECT,INSERT ON graph_document_selections,revision_evidence_sets,'
      'graph_evidence_orders,graph_state_contents,sealed_entity_revisions,sealed_relation_revisions,graph_snapshot_cache,graph_version_snapshots TO %s',role_name);
  END LOOP;
  FOR grant_row IN SELECT DISTINCT a.grantee FROM pg_class c
    CROSS JOIN LATERAL aclexplode(c.relacl) a WHERE c.oid='graph_states'::regclass
      AND a.privilege_type='SELECT' LOOP
    role_name:=CASE WHEN grant_row.grantee=0 THEN 'PUBLIC' ELSE quote_ident(pg_get_userbyid(grant_row.grantee)) END;
    EXECUTE format('GRANT SELECT ON graph_document_selections,revision_evidence_sets,'
      'graph_evidence_orders,graph_state_contents,sealed_entity_revisions,sealed_relation_revisions,graph_snapshot_cache,graph_version_snapshots TO %s',role_name);
  END LOOP;
END $$;
