"""Хранение согласования документов до появления нормализованного графа."""

from alembic import op

revision = "0008_pregraph_review"
down_revision = "0007_chat"
branch_labels = None
depends_on = None


def upgrade():
    op.execute("ALTER TABLE program_versions ALTER COLUMN graph_state_id DROP NOT NULL")
    op.execute("ALTER FUNCTION graph_snapshot(uuid,jsonb) RENAME TO graph_snapshot_with_graph")
    op.execute(
        """
        CREATE FUNCTION graph_snapshot(state_id uuid, version_review jsonb DEFAULT NULL)
        RETURNS jsonb LANGUAGE sql STABLE AS $$
        SELECT CASE WHEN state_id IS NULL THEN jsonb_build_object(
          'schema_version',2,'metadata','{}'::jsonb,'document_ids','[]'::jsonb,
          'entities','[]'::jsonb,'relations','[]'::jsonb,'source_refs','[]'::jsonb,
          'source_links','[]'::jsonb,'review',version_review)
        ELSE graph_snapshot_with_graph(state_id,version_review) END;
        $$;

        CREATE OR REPLACE FUNCTION graph_guard_job() RETURNS trigger LANGUAGE plpgsql
        SET search_path = pg_catalog, public, pg_temp AS $$
        BEGIN
          PERFORM graph_lock_program(NEW.program_id);
          IF TG_OP='UPDATE' AND (NEW.program_id<>OLD.program_id OR NEW.type<>OLD.type
            OR NEW.input_version_id<>OLD.input_version_id
            OR NEW.input_graph_state_id IS DISTINCT FROM OLD.input_graph_state_id
            OR (OLD.checked_version_id IS NOT NULL
                AND NEW.checked_version_id IS DISTINCT FROM OLD.checked_version_id))
          THEN RAISE EXCEPTION 'Вход операции неизменяем; создайте новую операцию'
            USING ERRCODE='23514'; END IF;
          PERFORM graph_require_sealed(NEW.program_id,NEW.input_graph_state_id);
          PERFORM graph_require_sealed(NEW.program_id,NEW.output_graph_state_id);
          IF NEW.output_version_id IS NOT NULL AND NOT EXISTS(
            SELECT 1 FROM program_versions
              WHERE id=NEW.output_version_id AND program_id=NEW.program_id
                AND graph_state_id IS NOT DISTINCT FROM NEW.output_graph_state_id)
          THEN RAISE EXCEPTION 'Выход операции не соответствует версии'
            USING ERRCODE='23514'; END IF;
          IF NEW.type='generate' AND NEW.input_graph_state_id IS NULL
          THEN RAISE EXCEPTION 'Для генерации нужен закреплённый граф'
            USING ERRCODE='23514'; END IF;
          RETURN NEW;
        END $$;
        """
    )


def downgrade():
    op.execute(
        """
        DO $$ BEGIN
          IF EXISTS(SELECT 1 FROM program_versions WHERE graph_state_id IS NULL) THEN
            RAISE EXCEPTION 'Нельзя удалить предграфовые версии до их обработки';
          END IF;
        END $$;
        DROP FUNCTION graph_snapshot(uuid,jsonb);
        ALTER FUNCTION graph_snapshot_with_graph(uuid,jsonb) RENAME TO graph_snapshot;
        ALTER TABLE program_versions ALTER COLUMN graph_state_id SET NOT NULL;

        CREATE OR REPLACE FUNCTION graph_guard_job() RETURNS trigger LANGUAGE plpgsql
        SET search_path = pg_catalog, public, pg_temp AS $$
        BEGIN
          PERFORM graph_lock_program(NEW.program_id);
          IF TG_OP='UPDATE' AND (NEW.program_id<>OLD.program_id OR NEW.type<>OLD.type
            OR NEW.input_version_id<>OLD.input_version_id
            OR NEW.input_graph_state_id IS DISTINCT FROM OLD.input_graph_state_id
            OR (OLD.checked_version_id IS NOT NULL
                AND NEW.checked_version_id IS DISTINCT FROM OLD.checked_version_id))
          THEN RAISE EXCEPTION 'Вход операции неизменяем; создайте новую операцию'
            USING ERRCODE='23514'; END IF;
          PERFORM graph_require_sealed(NEW.program_id,NEW.input_graph_state_id);
          PERFORM graph_require_sealed(NEW.program_id,NEW.output_graph_state_id);
          IF NEW.output_version_id IS NOT NULL
            AND (NEW.output_graph_state_id IS NULL OR NOT EXISTS(
              SELECT 1 FROM program_versions
                WHERE id=NEW.output_version_id AND program_id=NEW.program_id
                  AND graph_state_id=NEW.output_graph_state_id))
          THEN RAISE EXCEPTION 'Выход операции не соответствует версии'
            USING ERRCODE='23514'; END IF;
          IF NEW.type='generate' AND NEW.input_graph_state_id IS NULL
          THEN RAISE EXCEPTION 'Для генерации нужен закреплённый граф'
            USING ERRCODE='23514'; END IF;
          RETURN NEW;
        END $$;
        """
    )
