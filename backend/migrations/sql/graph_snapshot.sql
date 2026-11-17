-- Сборка контракта выполняется при чтении, без второй постоянной копии графа.
CREATE FUNCTION graph_snapshot(state_id uuid, version_review jsonb DEFAULT NULL)
RETURNS jsonb LANGUAGE sql STABLE AS $$
SELECT jsonb_build_object(
  'schema_version',s.schema_version,
  'metadata', (s.metadata - '__parsed_documents') || CASE WHEN s.metadata ? '__parsed_documents'
    THEN jsonb_build_object('parsed_documents',COALESCE((SELECT jsonb_object_agg(d.document_id::text,a.parsed_output)
      FROM graph_documents d JOIN document_artifacts a ON a.id=d.parsed_artifact_id
      WHERE d.graph_state_id=s.id),'{}'::jsonb)) ELSE '{}'::jsonb END,
  'document_ids',COALESCE((SELECT jsonb_agg(d.document_id ORDER BY d.position)
    FROM graph_documents d WHERE d.graph_state_id=s.id),'[]'::jsonb),
  'entities',COALESCE((SELECT jsonb_agg(jsonb_build_object('id',g.entity_id,'kind',r.kind,
    'attributes',r.attributes,'source_ref_ids',COALESCE((SELECT jsonb_agg(rs.source_ref_id ORDER BY rs.position)
      FROM revision_sources rs WHERE rs.entity_revision_id=r.revision_id),'[]'::jsonb)) ORDER BY g.position)
    FROM graph_entities g JOIN entity_revisions r ON r.revision_id=g.revision_id
    WHERE g.graph_state_id=s.id),'[]'::jsonb),
  'relations',COALESCE((SELECT jsonb_agg(jsonb_build_object('id',g.relation_id,
    'source_id',r.source_entity_id,'target_id',r.target_entity_id,'type',r.type,
    'confidence',CASE WHEN r.confidence IS NULL THEN 'null'::jsonb
      ELSE (r.confidence::text || CASE WHEN r.confidence=trunc(r.confidence)
        THEN '.0' ELSE '' END)::jsonb END,
    'source_ref_ids',COALESCE((SELECT jsonb_agg(rs.source_ref_id ORDER BY rs.position)
      FROM revision_sources rs WHERE rs.relation_revision_id=r.revision_id),'[]'::jsonb))
      || CASE WHEN s.schema_version=2 THEN jsonb_build_object('attributes',r.attributes) ELSE '{}'::jsonb END ORDER BY g.position)
    FROM graph_relations g JOIN relation_revisions r ON r.revision_id=g.revision_id
    WHERE g.graph_state_id=s.id),'[]'::jsonb),
  'source_refs',COALESCE((SELECT jsonb_agg(jsonb_build_object('id',r.id,'document_id',r.document_id,
    'section',r.section,'locator',r.locator) || CASE WHEN s.schema_version=2
    THEN jsonb_build_object('artifact_id',r.artifact_id,'artifact_locator',r.artifact_locator)
    ELSE '{}'::jsonb END ORDER BY g.position)
    FROM graph_sources g JOIN source_refs r ON r.id=g.source_ref_id
    WHERE g.graph_state_id=s.id),'[]'::jsonb)
) || CASE WHEN s.schema_version=2 THEN jsonb_build_object('review',version_review,
  'source_links',COALESCE((SELECT jsonb_agg(jsonb_build_object('id',g.public_id,
    'target_kind',CASE WHEN r.entity_id IS NULL THEN 'relation' ELSE 'entity' END,
    'target_id',COALESCE(r.entity_id,r.relation_id),'field_path',r.field_path,
    'source_ref_id',r.source_ref_id,'observed_value',r.observed_value) ORDER BY g.position)
    FROM graph_source_links g JOIN source_links r ON r.id=g.source_link_id
    WHERE g.graph_state_id=s.id),'[]'::jsonb)) ELSE '{}'::jsonb END
FROM graph_states s WHERE s.id=state_id;
$$;
