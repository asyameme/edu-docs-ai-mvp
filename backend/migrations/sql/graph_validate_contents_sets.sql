CREATE OR REPLACE FUNCTION graph_validate_contents(sid uuid) RETURNS void LANGUAGE plpgsql
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
    OR EXISTS(SELECT source_ref_id FROM required_refs EXCEPT SELECT id FROM refs)
    OR EXISTS(SELECT entity_revision_id FROM sets WHERE entity_revision_id IS NOT NULL
      EXCEPT SELECT id FROM ents)
    OR EXISTS(SELECT relation_revision_id FROM sets WHERE relation_revision_id IS NOT NULL
      EXCEPT SELECT id FROM rels)
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
