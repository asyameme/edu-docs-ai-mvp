CREATE OR REPLACE FUNCTION graph_fill_snapshot_cache() RETURNS trigger LANGUAGE plpgsql
SET search_path=pg_catalog,public,pg_temp AS $$
DECLARE pid uuid; graph_status text;
BEGIN
  SELECT program_id,status INTO pid,graph_status FROM graph_states WHERE id=NEW.graph_state_id;
  IF NOT FOUND THEN RAISE EXCEPTION 'Состояние не найдено' USING ERRCODE='23503'; END IF;
  PERFORM graph_lock_program(pid);
  IF graph_status<>'sealed' AND NOT EXISTS(
    SELECT 1 FROM graph_state_contents WHERE graph_state_id=NEW.graph_state_id)
  THEN RAISE EXCEPTION 'Изменяемый состав нельзя кешировать' USING ERRCODE='23514'; END IF;
  -- Переданный клиентом JSON всегда заменяется результатом сборки из состава.
  NEW.payload:=graph_build_snapshot(NEW.graph_state_id,NULL);
  RETURN NEW;
END $$;
CREATE TRIGGER fill_snapshot_cache BEFORE INSERT ON graph_snapshot_cache
FOR EACH ROW EXECUTE FUNCTION graph_fill_snapshot_cache();

CREATE OR REPLACE FUNCTION graph_seal_snapshot_cache() RETURNS trigger LANGUAGE plpgsql
SET search_path=pg_catalog,public,pg_temp AS $$
BEGIN
  INSERT INTO graph_snapshot_cache(graph_state_id)
    SELECT NEW.id WHERE NOT EXISTS(SELECT 1 FROM graph_snapshot_cache WHERE graph_state_id=NEW.id)
    ON CONFLICT DO NOTHING;
  RETURN NEW;
END $$;
CREATE TRIGGER seal_snapshot_cache AFTER UPDATE ON graph_states
FOR EACH ROW WHEN(NEW.status='sealed') EXECUTE FUNCTION graph_seal_snapshot_cache();

CREATE OR REPLACE FUNCTION graph_snapshot_with_graph(state_id uuid,version_review jsonb DEFAULT NULL)
RETURNS jsonb LANGUAGE sql STABLE SET search_path=pg_catalog,public,pg_temp AS $$
SELECT CASE WHEN v.payload IS NOT NULL THEN v.payload
  WHEN c.graph_state_id IS NULL THEN graph_build_snapshot(s.id,version_review)
  WHEN s.schema_version<>2 OR version_review IS NULL THEN c.payload
  ELSE jsonb_set(c.payload,'{review}',version_review) END
FROM graph_states s LEFT JOIN graph_snapshot_cache c ON c.graph_state_id=s.id
LEFT JOIN graph_version_snapshots v ON v.graph_state_id=s.id AND v.review=version_review
  AND v.review_hash=encode(sha256(convert_to(version_review::text,'UTF8')),'hex')
WHERE s.id=state_id;
$$;

CREATE OR REPLACE FUNCTION graph_fill_version_snapshot() RETURNS trigger LANGUAGE plpgsql
SET search_path=pg_catalog,public,pg_temp AS $$
BEGIN
  IF NOT EXISTS(SELECT 1 FROM graph_states WHERE id=NEW.graph_state_id AND status='sealed')
  THEN RAISE EXCEPTION 'Представление версии требует sealed' USING ERRCODE='23514'; END IF;
  NEW.review_hash:=encode(sha256(convert_to(NEW.review::text,'UTF8')),'hex');
  NEW.payload:=graph_snapshot_with_graph(NEW.graph_state_id,NEW.review);
  RETURN NEW;
END $$;
CREATE TRIGGER fill_version_snapshot BEFORE INSERT ON graph_version_snapshots
FOR EACH ROW EXECUTE FUNCTION graph_fill_version_snapshot();

CREATE OR REPLACE FUNCTION graph_cache_version() RETURNS trigger LANGUAGE plpgsql
SET search_path=pg_catalog,public,pg_temp AS $$
BEGIN
  IF NEW.graph_state_id IS NOT NULL AND NEW.review IS NOT NULL AND EXISTS(
    SELECT 1 FROM graph_states WHERE id=NEW.graph_state_id AND program_id=NEW.program_id
      AND schema_version=2 AND status='sealed') THEN
    INSERT INTO graph_version_snapshots(graph_state_id,review)
    SELECT NEW.graph_state_id,NEW.review WHERE NOT EXISTS(
      SELECT 1 FROM graph_version_snapshots WHERE graph_state_id=NEW.graph_state_id
        AND review_hash=encode(sha256(convert_to(NEW.review::text,'UTF8')),'hex'))
    ON CONFLICT DO NOTHING;
  END IF;
  RETURN NEW;
END $$;
CREATE TRIGGER cache_version_graph BEFORE INSERT ON program_versions
FOR EACH ROW EXECUTE FUNCTION graph_cache_version();
