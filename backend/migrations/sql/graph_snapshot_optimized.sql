-- Агрегируем источники выбранных редакций и собираем итоговый JSON один раз.
CREATE OR REPLACE FUNCTION graph_snapshot_with_graph(state_id uuid, version_review jsonb DEFAULT NULL)
RETURNS jsonb LANGUAGE sql STABLE
SET search_path = pg_catalog, public, pg_temp AS $$
WITH entity_sources AS MATERIALIZED (
  SELECT rs.entity_revision_id AS revision_id,
    jsonb_agg(rs.source_ref_id ORDER BY rs.position) AS refs
  FROM graph_entities g JOIN revision_sources rs ON rs.entity_revision_id=g.revision_id
  WHERE g.graph_state_id=state_id
  GROUP BY rs.entity_revision_id
), relation_sources AS MATERIALIZED (
  SELECT rs.relation_revision_id AS revision_id,
    jsonb_agg(rs.source_ref_id ORDER BY rs.position) AS refs
  FROM graph_relations g JOIN revision_sources rs ON rs.relation_revision_id=g.revision_id
  WHERE g.graph_state_id=state_id
  GROUP BY rs.relation_revision_id
), evidence AS (
  -- Большой массив свидетельств приводится к JSONB только вместе с итоговым объектом.
  SELECT json_agg(json_build_object('id',g.public_id,
    'target_kind',CASE WHEN r.entity_id IS NULL THEN 'relation' ELSE 'entity' END,
    'target_id',COALESCE(r.entity_id,r.relation_id),'field_path',r.field_path,
    'source_ref_id',r.source_ref_id,'observed_value',r.observed_value) ORDER BY g.position) AS links
  FROM graph_source_links g JOIN source_links r ON r.id=g.source_link_id
  WHERE g.graph_state_id=state_id
), assembled AS (
SELECT json_build_object(
  'schema_version',s.schema_version,
  'metadata', (s.metadata - '__parsed_documents') || CASE WHEN s.metadata ? '__parsed_documents'
    THEN jsonb_build_object('parsed_documents',COALESCE((SELECT jsonb_object_agg(d.document_id::text,a.parsed_output)
      FROM graph_documents d JOIN document_artifacts a ON a.id=d.parsed_artifact_id
      WHERE d.graph_state_id=s.id),'{}'::jsonb)) ELSE '{}'::jsonb END,
  'document_ids',COALESCE((SELECT jsonb_agg(d.document_id ORDER BY d.position)
    FROM graph_documents d WHERE d.graph_state_id=s.id),'[]'::jsonb),
  'entities',COALESCE((SELECT jsonb_agg(jsonb_build_object('id',g.entity_id,'kind',r.kind,
    'attributes',r.attributes,'source_ref_ids',COALESCE(es.refs,'[]'::jsonb)) ORDER BY g.position)
    FROM graph_entities g JOIN entity_revisions r ON r.revision_id=g.revision_id
    LEFT JOIN entity_sources es ON es.revision_id=g.revision_id
    WHERE g.graph_state_id=s.id),'[]'::jsonb),
  'relations',COALESCE((SELECT jsonb_agg(jsonb_build_object('id',g.relation_id,
    'source_id',r.source_entity_id,'target_id',r.target_entity_id,'type',r.type,
    'confidence',CASE WHEN r.confidence IS NULL THEN 'null'::jsonb
      ELSE (r.confidence::text || CASE WHEN r.confidence=trunc(r.confidence)
        THEN '.0' ELSE '' END)::jsonb END,
    'source_ref_ids',COALESCE(rs.refs,'[]'::jsonb))
      || CASE WHEN s.schema_version=2 THEN jsonb_build_object('attributes',r.attributes) ELSE '{}'::jsonb END ORDER BY g.position)
    FROM graph_relations g JOIN relation_revisions r ON r.revision_id=g.revision_id
    LEFT JOIN relation_sources rs ON rs.revision_id=g.revision_id
    WHERE g.graph_state_id=s.id),'[]'::jsonb),
  'source_refs',COALESCE((SELECT jsonb_agg(jsonb_build_object('id',r.id,'document_id',r.document_id,
    'section',r.section,'locator',r.locator) || CASE WHEN s.schema_version=2
    THEN jsonb_build_object('artifact_id',r.artifact_id,'artifact_locator',r.artifact_locator)
    ELSE '{}'::jsonb END ORDER BY g.position)
    FROM graph_sources g JOIN source_refs r ON r.id=g.source_ref_id
    WHERE g.graph_state_id=s.id),'[]'::jsonb),
  'review',version_review,
  'source_links',CASE WHEN s.schema_version=2
    THEN COALESCE((SELECT links FROM evidence),'[]'::json) ELSE '[]'::json END
)::jsonb AS snapshot,s.schema_version
FROM graph_states s WHERE s.id=state_id
)
-- V2 собирается одним объектом: конкатенация повторно копировала весь большой граф.
SELECT CASE WHEN schema_version=2 THEN snapshot
  ELSE snapshot - ARRAY['review','source_links'] END
FROM assembled;
$$;
