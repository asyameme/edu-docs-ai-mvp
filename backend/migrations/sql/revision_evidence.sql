-- Принадлежность свидетельств неизменяемому варианту редакции, а не состоянию.
CREATE TABLE graph_document_selections (
  id uuid PRIMARY KEY, program_id uuid NOT NULL, document_id uuid NOT NULL,
  parsed_artifact_id uuid, parsed_artifact_kind text NOT NULL CHECK(parsed_artifact_kind='parsed_json'),
  member_name text NOT NULL, UNIQUE(program_id,id),
  FOREIGN KEY(program_id,document_id) REFERENCES source_documents(program_id,id),
  FOREIGN KEY(program_id,document_id,parsed_artifact_id,parsed_artifact_kind)
    REFERENCES document_artifacts(program_id,document_id,id,kind)
);
CREATE TABLE revision_evidence_sets (
  id uuid PRIMARY KEY, program_id uuid NOT NULL,
  entity_revision_id uuid, relation_revision_id uuid,
  source_link_ids uuid[] NOT NULL, public_ids uuid[] NOT NULL,
  UNIQUE(program_id,id), CHECK((entity_revision_id IS NULL)<>(relation_revision_id IS NULL)),
  CHECK(cardinality(source_link_ids)>0 AND cardinality(source_link_ids)=cardinality(public_ids)),
  CHECK(array_position(source_link_ids,NULL) IS NULL AND array_position(public_ids,NULL) IS NULL),
  CHECK(array_ndims(source_link_ids)=1 AND array_ndims(public_ids)=1)
);
CREATE TABLE graph_evidence_orders (
  id uuid PRIMARY KEY, program_id uuid NOT NULL REFERENCES programs(id),
  public_ids uuid[] NOT NULL, UNIQUE(program_id,id),
  CHECK(cardinality(public_ids)>0 AND array_position(public_ids,NULL) IS NULL),
  CHECK(array_ndims(public_ids)=1)
);
CREATE TABLE graph_state_contents (
  graph_state_id uuid PRIMARY KEY, program_id uuid NOT NULL,
  entity_revision_ids uuid[] NOT NULL, relation_revision_ids uuid[] NOT NULL,
  document_selection_ids uuid[] NOT NULL, source_ref_ids uuid[] NOT NULL,
  evidence_set_ids uuid[] NOT NULL, evidence_order_id uuid,
  FOREIGN KEY(program_id,graph_state_id) REFERENCES graph_states(program_id,id),
  FOREIGN KEY(program_id,evidence_order_id) REFERENCES graph_evidence_orders(program_id,id),
  CHECK((cardinality(evidence_set_ids)=0)=(evidence_order_id IS NULL)),
  CHECK(array_position(entity_revision_ids,NULL) IS NULL AND array_position(relation_revision_ids,NULL) IS NULL
    AND array_position(document_selection_ids,NULL) IS NULL AND array_position(source_ref_ids,NULL) IS NULL
    AND array_position(evidence_set_ids,NULL) IS NULL),
  CHECK(COALESCE(array_ndims(entity_revision_ids),1)=1 AND COALESCE(array_ndims(relation_revision_ids),1)=1
    AND COALESCE(array_ndims(document_selection_ids),1)=1 AND COALESCE(array_ndims(source_ref_ids),1)=1
    AND COALESCE(array_ndims(evidence_set_ids),1)=1)
);
CREATE TABLE sealed_entity_revisions (
  revision_id uuid PRIMARY KEY
);
CREATE TABLE sealed_relation_revisions (
  revision_id uuid PRIMARY KEY
);
-- Производное представление проверенного состава; не источник истории или UUID.
CREATE TABLE graph_snapshot_cache (
  graph_state_id uuid PRIMARY KEY REFERENCES graph_states(id),
  payload jsonb NOT NULL
);
CREATE TABLE graph_version_snapshots (
  graph_state_id uuid NOT NULL REFERENCES graph_states(id),
  review_hash text NOT NULL, review jsonb NOT NULL CHECK(jsonb_typeof(review)='object'),
  payload jsonb NOT NULL, PRIMARY KEY(graph_state_id,review_hash)
);
DO $$ BEGIN
  IF EXISTS(SELECT 1 FROM pg_settings WHERE name='default_toast_compression'
    AND 'lz4'=ANY(enumvals)) THEN
    ALTER TABLE graph_snapshot_cache ALTER COLUMN payload SET COMPRESSION lz4;
    ALTER TABLE graph_version_snapshots ALTER COLUMN payload SET COMPRESSION lz4;
  END IF;
END $$;

CREATE FUNCTION graph_guard_evidence_set() RETURNS trigger LANGUAGE plpgsql
SET search_path=pg_catalog,public,pg_temp AS $$
DECLARE target_entity uuid; target_relation uuid;
BEGIN
  PERFORM graph_lock_program(NEW.program_id);
  IF NEW.entity_revision_id IS NOT NULL THEN
    SELECT entity_id INTO target_entity FROM entity_revisions
      WHERE program_id=NEW.program_id AND revision_id=NEW.entity_revision_id;
  ELSE
    SELECT relation_id INTO target_relation FROM relation_revisions
      WHERE program_id=NEW.program_id AND revision_id=NEW.relation_revision_id;
  END IF;
  IF cardinality(NEW.source_link_ids)<>(SELECT count(DISTINCT x) FROM unnest(NEW.source_link_ids) x)
    OR cardinality(NEW.public_ids)<>(SELECT count(DISTINCT x) FROM unnest(NEW.public_ids) x)
  THEN RAISE EXCEPTION 'Повтор свидетельства в наборе' USING ERRCODE='23514'; END IF;
  IF EXISTS(SELECT 1 FROM unnest(NEW.source_link_ids) x
    LEFT JOIN source_links l ON l.id=x AND l.program_id=NEW.program_id
    WHERE l.id IS NULL OR l.entity_id IS DISTINCT FROM target_entity
      OR l.relation_id IS DISTINCT FROM target_relation)
  THEN RAISE EXCEPTION 'Свидетельство не принадлежит редакции' USING ERRCODE='23503'; END IF;
  RETURN NEW;
END $$;
CREATE FUNCTION graph_guard_evidence_order() RETURNS trigger LANGUAGE plpgsql
SET search_path=pg_catalog,public,pg_temp AS $$
BEGIN
  IF cardinality(NEW.public_ids)<>(SELECT count(DISTINCT x) FROM unnest(NEW.public_ids) x)
  THEN RAISE EXCEPTION 'Повтор публичного ID в порядке свидетельств' USING ERRCODE='23514'; END IF;
  RETURN NEW;
END $$;

CREATE FUNCTION graph_check_evidence_sets() RETURNS trigger LANGUAGE plpgsql
SET search_path=pg_catalog,public,pg_temp AS $$
DECLARE pid uuid; invalid boolean;
BEGIN
  FOR pid IN SELECT DISTINCT program_id FROM inserted_evidence_sets ORDER BY program_id LOOP
    PERFORM graph_lock_program(pid);
  END LOOP;
  IF EXISTS(SELECT 1 FROM inserted_evidence_sets e
    WHERE cardinality(e.source_link_ids)<>(SELECT count(DISTINCT x) FROM unnest(e.source_link_ids) x)
      OR cardinality(e.public_ids)<>(SELECT count(DISTINCT x) FROM unnest(e.public_ids) x))
  THEN RAISE EXCEPTION 'Повтор свидетельства в наборе' USING ERRCODE='23514'; END IF;
  WITH owners AS MATERIALIZED (
    SELECT e.program_id,e.source_link_ids,r.entity_id,NULL::uuid AS relation_id
    FROM inserted_evidence_sets e JOIN entity_revisions r
      ON r.revision_id=e.entity_revision_id AND r.program_id=e.program_id
    UNION ALL
    SELECT e.program_id,e.source_link_ids,NULL::uuid,r.relation_id
    FROM inserted_evidence_sets e JOIN relation_revisions r
      ON r.revision_id=e.relation_revision_id AND r.program_id=e.program_id
  ), expected AS MATERIALIZED (
    SELECT o.program_id,x.id,o.entity_id,o.relation_id
    FROM owners o CROSS JOIN LATERAL unnest(o.source_link_ids) x(id)
  )
  SELECT (SELECT count(*) FROM owners)<>(SELECT count(*) FROM inserted_evidence_sets)
    OR EXISTS(
      SELECT program_id,id,entity_id,relation_id FROM expected
      EXCEPT SELECT program_id,id,entity_id,relation_id FROM source_links
        WHERE id IN (SELECT id FROM expected)
    ) INTO invalid;
  IF invalid THEN RAISE EXCEPTION 'Свидетельство не принадлежит редакции' USING ERRCODE='23503'; END IF;
  RETURN NULL;
END $$;

CREATE FUNCTION graph_check_revision_seals() RETURNS trigger LANGUAGE plpgsql
SET search_path=pg_catalog,public,pg_temp AS $$
BEGIN
  IF TG_TABLE_NAME='sealed_entity_revisions' THEN
    IF EXISTS(SELECT 1 FROM inserted_sealed_revisions x LEFT JOIN entity_revisions r
      ON r.revision_id=x.revision_id WHERE r.revision_id IS NULL)
    THEN RAISE EXCEPTION 'Редакция отсутствует' USING ERRCODE='23503'; END IF;
  ELSE
    IF EXISTS(SELECT 1 FROM inserted_sealed_revisions x LEFT JOIN relation_revisions r
      ON r.revision_id=x.revision_id WHERE r.revision_id IS NULL)
    THEN RAISE EXCEPTION 'Редакция отсутствует' USING ERRCODE='23503'; END IF;
  END IF;
  RETURN NULL;
END $$;

-- Все ссылки массивов проверяются соединениями, без триггера на каждый элемент.
CREATE FUNCTION graph_validate_contents(sid uuid) RETURNS void LANGUAGE plpgsql
SET search_path=pg_catalog,public,pg_temp AS $$
DECLARE c graph_state_contents; invalid boolean;
BEGIN
  SELECT * INTO c FROM graph_state_contents WHERE graph_state_id=sid;
  IF NOT FOUND THEN RAISE EXCEPTION 'Состав состояния отсутствует' USING ERRCODE='23503'; END IF;
  WITH ents AS MATERIALIZED (
    SELECT x.id,r.entity_id,r.program_id FROM unnest(c.entity_revision_ids) x(id)
    LEFT JOIN entity_revisions r ON r.revision_id=x.id
  ), rels AS MATERIALIZED (
    SELECT x.id,r.relation_id,r.source_entity_id,r.target_entity_id,r.program_id
    FROM unnest(c.relation_revision_ids) x(id) LEFT JOIN relation_revisions r ON r.revision_id=x.id
  ), docs AS MATERIALIZED (
    SELECT x.id,d.document_id,d.program_id FROM unnest(c.document_selection_ids) x(id)
    LEFT JOIN graph_document_selections d ON d.id=x.id
  ), refs AS MATERIALIZED (
    SELECT x.id,r.document_id,r.program_id FROM unnest(c.source_ref_ids) x(id)
    LEFT JOIN source_refs r ON r.id=x.id
  ), sets AS MATERIALIZED (
    SELECT x.id,e.program_id,e.entity_revision_id,e.relation_revision_id,e.source_link_ids,e.public_ids
    FROM unnest(c.evidence_set_ids) x(id) LEFT JOIN revision_evidence_sets e ON e.id=x.id
  ), facts AS MATERIALIZED (
    SELECT p.public_id,l.id,l.source_ref_id FROM sets e
    CROSS JOIN LATERAL unnest(e.source_link_ids,e.public_ids) p(fact_id,public_id)
    JOIN source_links l ON l.id=p.fact_id
  ), required_refs AS (
    SELECT r.source_ref_id FROM ents e JOIN revision_sources r ON r.entity_revision_id=e.id
    UNION ALL SELECT r.source_ref_id FROM rels e JOIN revision_sources r ON r.relation_revision_id=e.id
  )
  SELECT
    EXISTS(SELECT 1 FROM ents WHERE program_id IS DISTINCT FROM c.program_id)
    OR EXISTS(SELECT 1 FROM rels WHERE program_id IS DISTINCT FROM c.program_id)
    OR EXISTS(SELECT 1 FROM docs WHERE program_id IS DISTINCT FROM c.program_id)
    OR EXISTS(SELECT 1 FROM refs WHERE program_id IS DISTINCT FROM c.program_id)
    OR EXISTS(SELECT 1 FROM sets WHERE program_id IS DISTINCT FROM c.program_id)
    OR (SELECT count(*)<>count(DISTINCT entity_id) FROM ents)
    OR (SELECT count(*)<>count(DISTINCT relation_id) FROM rels)
    OR (SELECT count(*)<>count(DISTINCT document_id) FROM docs)
    OR (SELECT count(*)<>count(DISTINCT id) FROM refs)
    OR (SELECT count(*)<>count(DISTINCT (entity_revision_id,relation_revision_id)) FROM sets)
    OR EXISTS(SELECT 1 FROM rels r LEFT JOIN ents a ON a.entity_id=r.source_entity_id
      LEFT JOIN ents b ON b.entity_id=r.target_entity_id WHERE a.id IS NULL OR b.id IS NULL)
    OR EXISTS(SELECT 1 FROM refs r LEFT JOIN docs d ON d.document_id=r.document_id WHERE d.id IS NULL)
    OR EXISTS(SELECT 1 FROM required_refs r LEFT JOIN refs f ON f.id=r.source_ref_id WHERE f.id IS NULL)
    OR EXISTS(SELECT 1 FROM sets e LEFT JOIN ents a ON a.id=e.entity_revision_id
      LEFT JOIN rels b ON b.id=e.relation_revision_id WHERE a.id IS NULL AND b.id IS NULL)
    OR EXISTS(SELECT 1 FROM facts l LEFT JOIN refs r ON r.id=l.source_ref_id WHERE r.id IS NULL)
    OR (SELECT count(*)<>count(DISTINCT public_id) FROM facts)
    OR (SELECT count(*)<>count(DISTINCT id) FROM facts)
    OR COALESCE((SELECT cardinality(o.public_ids) FROM graph_evidence_orders o
       WHERE o.id=c.evidence_order_id),0)<>(SELECT count(*) FROM facts)
    OR EXISTS(SELECT 1 FROM graph_evidence_orders o CROSS JOIN LATERAL unnest(o.public_ids) p(id)
      LEFT JOIN facts f ON f.public_id=p.id WHERE o.id=c.evidence_order_id AND f.id IS NULL)
  INTO invalid;
  IF invalid THEN RAISE EXCEPTION 'Неверные ссылки компактного состава графа' USING ERRCODE='23503'; END IF;
END $$;

CREATE FUNCTION graph_guard_contents() RETURNS trigger LANGUAGE plpgsql
SET search_path=pg_catalog,public,pg_temp AS $$
BEGIN
  PERFORM graph_lock_program(NEW.program_id);
  IF NOT EXISTS(SELECT 1 FROM graph_states WHERE id=NEW.graph_state_id
    AND program_id=NEW.program_id AND status='building')
  THEN RAISE EXCEPTION 'Состав создаётся только у building' USING ERRCODE='23514'; END IF;
  IF EXISTS(SELECT 1 FROM graph_entities_legacy WHERE graph_state_id=NEW.graph_state_id)
    OR EXISTS(SELECT 1 FROM graph_relations_legacy WHERE graph_state_id=NEW.graph_state_id)
    OR EXISTS(SELECT 1 FROM graph_documents_legacy WHERE graph_state_id=NEW.graph_state_id)
    OR EXISTS(SELECT 1 FROM graph_sources_legacy WHERE graph_state_id=NEW.graph_state_id)
    OR EXISTS(SELECT 1 FROM graph_source_links_legacy WHERE graph_state_id=NEW.graph_state_id)
  THEN RAISE EXCEPTION 'Два состава одного состояния запрещены' USING ERRCODE='23514'; END IF;
  RETURN NEW;
END $$;
CREATE FUNCTION graph_check_contents() RETURNS trigger LANGUAGE plpgsql
SET search_path=pg_catalog,public,pg_temp AS $$
BEGIN
  PERFORM graph_validate_contents(NEW.graph_state_id);
  -- Неизменяемый состав замораживает источники редакций ещё до чтения и seal.
  INSERT INTO sealed_entity_revisions SELECT x.id FROM unnest(NEW.entity_revision_ids) x(id)
    LEFT JOIN sealed_entity_revisions s ON s.revision_id=x.id WHERE s.revision_id IS NULL
    ON CONFLICT DO NOTHING;
  INSERT INTO sealed_relation_revisions SELECT x.id FROM unnest(NEW.relation_revision_ids) x(id)
    LEFT JOIN sealed_relation_revisions s ON s.revision_id=x.id WHERE s.revision_id IS NULL
    ON CONFLICT DO NOTHING;
  RETURN NEW;
END $$;

CREATE OR REPLACE FUNCTION graph_guard_membership() RETURNS trigger LANGUAGE plpgsql
SET search_path=pg_catalog,public,pg_temp AS $$
DECLARE p uuid; s uuid;
BEGIN
  IF TG_OP='DELETE' THEN p:=OLD.program_id; s:=OLD.graph_state_id;
  ELSE p:=NEW.program_id; s:=NEW.graph_state_id; END IF;
  PERFORM graph_lock_program(p);
  IF TG_OP='UPDATE' AND (NEW.program_id<>OLD.program_id OR NEW.graph_state_id<>OLD.graph_state_id)
  THEN RAISE EXCEPTION 'Перенос состава между состояниями запрещён' USING ERRCODE='23514'; END IF;
  IF NOT EXISTS(SELECT 1 FROM graph_states WHERE id=s AND program_id=p AND status='building')
  THEN RAISE EXCEPTION 'Состав можно менять только у building' USING ERRCODE='23514'; END IF;
  IF EXISTS(SELECT 1 FROM graph_state_contents WHERE graph_state_id=s)
  THEN RAISE EXCEPTION 'Два состава одного состояния запрещены' USING ERRCODE='23514'; END IF;
  IF TG_OP='DELETE' THEN RETURN OLD; END IF;
  RETURN NEW;
END $$;

CREATE OR REPLACE FUNCTION graph_guard_revision_sources() RETURNS trigger LANGUAGE plpgsql
SET search_path=pg_catalog,public,pg_temp AS $$
BEGIN
  PERFORM graph_lock_program(NEW.program_id);
  IF EXISTS(SELECT 1 FROM sealed_entity_revisions WHERE revision_id=NEW.entity_revision_id)
    OR EXISTS(SELECT 1 FROM sealed_relation_revisions WHERE revision_id=NEW.relation_revision_id)
  THEN RAISE EXCEPTION 'Нельзя дополнять источники запечатанной редакции' USING ERRCODE='23514'; END IF;
  RETURN NEW;
END $$;

CREATE OR REPLACE FUNCTION graph_guard_state() RETURNS trigger LANGUAGE plpgsql
SET search_path=pg_catalog,public,pg_temp AS $$
DECLARE compact boolean;
BEGIN
  IF TG_OP='DELETE' THEN
    PERFORM graph_lock_program(OLD.program_id);
    IF OLD.status='sealed' THEN RAISE EXCEPTION 'Удаление sealed запрещено' USING ERRCODE='23514'; END IF;
    RETURN OLD;
  END IF;
  PERFORM graph_lock_program(NEW.program_id);
  IF TG_OP='INSERT' AND NEW.status<>'building'
  THEN RAISE EXCEPTION 'Новое состояние начинается с building' USING ERRCODE='23514'; END IF;
  IF TG_OP='UPDATE' AND (OLD.status='sealed' OR NEW.id<>OLD.id OR NEW.program_id<>OLD.program_id)
  THEN RAISE EXCEPTION 'Изменение sealed или идентичности запрещено' USING ERRCODE='23514'; END IF;
  IF TG_OP='UPDATE' AND (NEW.metadata IS DISTINCT FROM OLD.metadata
    OR NEW.schema_version IS DISTINCT FROM OLD.schema_version) THEN
    IF EXISTS(SELECT 1 FROM graph_state_contents WHERE graph_state_id=NEW.id)
      OR EXISTS(SELECT 1 FROM graph_snapshot_cache WHERE graph_state_id=NEW.id)
    THEN RAISE EXCEPTION 'Метаданные замороженного состава неизменяемы' USING ERRCODE='23514'; END IF;
  END IF;
  IF NEW.status='sealed' THEN
    compact := EXISTS(SELECT 1 FROM graph_state_contents WHERE graph_state_id=NEW.id);
    IF NOT compact THEN
      IF EXISTS(SELECT 1 FROM revision_sources r WHERE r.program_id=NEW.program_id
        AND (EXISTS(SELECT 1 FROM graph_entities_legacy g WHERE g.graph_state_id=NEW.id AND g.revision_id=r.entity_revision_id)
          OR EXISTS(SELECT 1 FROM graph_relations_legacy g WHERE g.graph_state_id=NEW.id AND g.revision_id=r.relation_revision_id))
        AND NOT EXISTS(SELECT 1 FROM graph_sources_legacy g WHERE g.graph_state_id=NEW.id AND g.source_ref_id=r.source_ref_id))
      THEN RAISE EXCEPTION 'Источники редакций не включены в граф' USING ERRCODE='23514'; END IF;
    END IF;
    IF NOT compact AND EXISTS(SELECT 1 FROM graph_documents g JOIN document_artifacts a ON a.id=g.parsed_artifact_id
      WHERE g.graph_state_id=NEW.id AND a.kind<>'parsed_json')
    THEN RAISE EXCEPTION 'Документ должен выбирать parsed_json' USING ERRCODE='23514'; END IF;
    IF NOT compact THEN
      INSERT INTO sealed_entity_revisions SELECT g.revision_id FROM graph_entities g
        LEFT JOIN sealed_entity_revisions existing_seal ON existing_seal.revision_id=g.revision_id
        WHERE g.graph_state_id=NEW.id AND existing_seal.revision_id IS NULL ON CONFLICT DO NOTHING;
      INSERT INTO sealed_relation_revisions SELECT g.revision_id FROM graph_relations g
        LEFT JOIN sealed_relation_revisions existing_seal ON existing_seal.revision_id=g.revision_id
        WHERE g.graph_state_id=NEW.id AND existing_seal.revision_id IS NULL ON CONFLICT DO NOTHING;
    END IF;
  END IF;
  RETURN NEW;
END $$;
