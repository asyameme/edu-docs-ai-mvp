-- Границы материализации не позволяют оценкам маленькой старой БД превратить
-- сборку нового большого состояния во вложенный обход свидетельств.
CREATE OR REPLACE FUNCTION graph_compact_snapshot(state_id uuid,version_review jsonb DEFAULT NULL)
RETURNS jsonb LANGUAGE sql STABLE SET search_path=pg_catalog,public,pg_temp
SET enable_nestloop=off SET jit=off AS $$
WITH c AS MATERIALIZED (
  SELECT g.*,s.schema_version,s.metadata FROM graph_state_contents g
  JOIN graph_states s ON s.id=g.graph_state_id WHERE g.graph_state_id=state_id
), ep AS MATERIALIZED (
  SELECT x.id,x.position FROM c CROSS JOIN LATERAL unnest(c.entity_revision_ids) WITH ORDINALITY x(id,position)
), rp AS MATERIALIZED (
  SELECT x.id,x.position FROM c CROSS JOIN LATERAL unnest(c.relation_revision_ids) WITH ORDINALITY x(id,position)
), dp AS MATERIALIZED (
  SELECT x.id,x.position FROM c CROSS JOIN LATERAL unnest(c.document_selection_ids) WITH ORDINALITY x(id,position)
), sp AS MATERIALIZED (
  SELECT x.id,x.position FROM c CROSS JOIN LATERAL unnest(c.source_ref_ids) WITH ORDINALITY x(id,position)
), ed AS MATERIALIZED (
  SELECT * FROM entity_revisions WHERE revision_id IN(SELECT id FROM ep)
), rd AS MATERIALIZED (
  SELECT * FROM relation_revisions WHERE revision_id IN(SELECT id FROM rp)
), dd AS MATERIALIZED (
  SELECT * FROM graph_document_selections WHERE id IN(SELECT id FROM dp)
), sd AS MATERIALIZED (
  SELECT * FROM source_refs WHERE id IN(SELECT id FROM sp)
), es AS MATERIALIZED (
  SELECT entity_revision_id,json_agg(source_ref_id ORDER BY position) AS refs
  FROM revision_sources WHERE entity_revision_id IN(SELECT id FROM ep) GROUP BY entity_revision_id
), rs AS MATERIALIZED (
  SELECT relation_revision_id,json_agg(source_ref_id ORDER BY position) AS refs
  FROM revision_sources WHERE relation_revision_id IN(SELECT id FROM rp) GROUP BY relation_revision_id
), selected_sets AS MATERIALIZED (
  SELECT * FROM revision_evidence_sets WHERE id IN(SELECT unnest(evidence_set_ids) FROM c)
), pairs AS MATERIALIZED (
  SELECT x.fact_id,x.public_id FROM selected_sets e
  CROSS JOIN LATERAL unnest(e.source_link_ids,e.public_ids) x(fact_id,public_id)
), facts AS MATERIALIZED (
  SELECT * FROM source_links WHERE id IN(SELECT fact_id FROM pairs)
), ordering AS MATERIALIZED (
  SELECT x.public_id,x.position FROM c JOIN graph_evidence_orders o ON o.id=c.evidence_order_id
  CROSS JOIN LATERAL unnest(o.public_ids) WITH ORDINALITY x(public_id,position)
), result AS (
 SELECT c.schema_version,json_build_object(
  'schema_version',c.schema_version,
  'metadata',(c.metadata-'__parsed_documents') || CASE WHEN c.metadata ? '__parsed_documents' THEN
    jsonb_build_object('parsed_documents',COALESCE((SELECT jsonb_object_agg(d.document_id::text,a.parsed_output)
      FROM dd d JOIN document_artifacts a ON a.id=d.parsed_artifact_id),'{}'::jsonb)) ELSE '{}'::jsonb END,
  'document_ids',COALESCE((SELECT json_agg(d.document_id ORDER BY p.position)
    FROM dp p JOIN dd d ON d.id=p.id),'[]'::json),
  'entities',COALESCE((SELECT json_agg(json_build_object('id',r.entity_id,'kind',r.kind,
    'attributes',r.attributes,'source_ref_ids',COALESCE(s.refs,'[]'::json)) ORDER BY p.position)
    FROM ep p JOIN ed r ON r.revision_id=p.id LEFT JOIN es s ON s.entity_revision_id=p.id),'[]'::json),
  'relations',COALESCE((SELECT json_agg((jsonb_build_object('id',r.relation_id,'source_id',r.source_entity_id,
    'target_id',r.target_entity_id,'type',r.type,
    'confidence',CASE WHEN r.confidence IS NULL THEN 'null'::jsonb ELSE
      (r.confidence::text || CASE WHEN r.confidence=trunc(r.confidence) THEN '.0' ELSE '' END)::jsonb END,
    'source_ref_ids',COALESCE(s.refs,'[]'::json)) || CASE WHEN c.schema_version=2
      THEN jsonb_build_object('attributes',r.attributes) ELSE '{}'::jsonb END) ORDER BY p.position)
    FROM rp p JOIN rd r ON r.revision_id=p.id LEFT JOIN rs s ON s.relation_revision_id=p.id),'[]'::json),
  'source_refs',COALESCE((SELECT json_agg((jsonb_build_object('id',r.id,'document_id',r.document_id,
    'section',r.section,'locator',r.locator) || CASE WHEN c.schema_version=2
      THEN jsonb_build_object('artifact_id',r.artifact_id,'artifact_locator',r.artifact_locator)
      ELSE '{}'::jsonb END) ORDER BY p.position) FROM sp p JOIN sd r ON r.id=p.id),'[]'::json),
  'review',version_review,
  'source_links',CASE WHEN c.schema_version=2 THEN COALESCE((SELECT json_agg(json_build_object(
    'id',p.public_id,'target_kind',CASE WHEN f.entity_id IS NULL THEN 'relation' ELSE 'entity' END,
    'target_id',COALESCE(f.entity_id,f.relation_id),'field_path',f.field_path,
    'source_ref_id',f.source_ref_id,'observed_value',f.observed_value) ORDER BY o.position)
    FROM pairs p JOIN facts f ON f.id=p.fact_id JOIN ordering o ON o.public_id=p.public_id),'[]'::json)
    ELSE '[]'::json END
 )::jsonb AS payload FROM c
)
SELECT CASE WHEN schema_version=2 THEN payload ELSE payload-ARRAY['review','source_links'] END FROM result;
$$;

CREATE OR REPLACE FUNCTION graph_build_snapshot(state_id uuid,version_review jsonb DEFAULT NULL)
RETURNS jsonb LANGUAGE sql STABLE SET search_path=pg_catalog,public,pg_temp AS $$
SELECT CASE WHEN EXISTS(SELECT 1 FROM graph_state_contents WHERE graph_state_id=state_id)
  THEN graph_compact_snapshot(state_id,version_review)
  ELSE graph_snapshot_legacy_graph(state_id,version_review) END;
$$;
