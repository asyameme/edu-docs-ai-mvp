-- Предграфовые версии хранят review без физического состояния графа.
CREATE OR REPLACE FUNCTION graph_snapshot(state_id uuid,version_review jsonb DEFAULT NULL)
RETURNS jsonb LANGUAGE sql STABLE SET search_path=pg_catalog,public,pg_temp AS $$
SELECT CASE WHEN state_id IS NULL THEN jsonb_build_object(
  'schema_version',2,'metadata','{}'::jsonb,'document_ids','[]'::jsonb,
  'entities','[]'::jsonb,'relations','[]'::jsonb,'source_refs','[]'::jsonb,
  'source_links','[]'::jsonb,'review',version_review)
ELSE graph_snapshot_with_graph(state_id,version_review) END;
$$;
