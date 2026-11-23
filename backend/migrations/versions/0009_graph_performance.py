"""Индексы запечатывания, пакетная сборка источников и удаление избыточного FK."""

from pathlib import Path

from alembic import op

revision = "0009_graph_performance"
down_revision = "0008_pregraph_review"
branch_labels = None
depends_on = None


def upgrade():
    op.execute(
        "CREATE INDEX graph_entities_state_revision_idx ON "
        "graph_entities(graph_state_id,revision_id)"
    )
    op.execute(
        "CREATE INDEX graph_relations_state_revision_idx ON "
        "graph_relations(graph_state_id,revision_id)"
    )
    # CHECK требует ровно одну цель; её составной FK уже проверяет program_id и факт.
    op.execute(
        "ALTER TABLE graph_source_links DROP CONSTRAINT "
        "graph_source_links_program_id_source_link_id_fkey"
    )
    sql = Path(__file__).parents[1] / "sql/graph_snapshot_optimized.sql"
    op.execute(sql.read_text(encoding="utf-8"))
    op.execute((sql.parent / "graph_snapshot_wrapper.sql").read_text(encoding="utf-8"))


def downgrade():
    sql = Path(__file__).parents[1] / "sql/graph_snapshot.sql"
    op.execute(
        sql.read_text(encoding="utf-8").replace(
            "CREATE FUNCTION graph_snapshot",
            "CREATE OR REPLACE FUNCTION graph_snapshot_with_graph",
            1,
        )
    )
    op.execute(
        "ALTER FUNCTION graph_snapshot_with_graph(uuid,jsonb) "
        "SET search_path=pg_catalog,public,pg_temp"
    )
    op.execute((sql.parent / "graph_snapshot_wrapper.sql").read_text(encoding="utf-8"))
    op.execute(
        "ALTER TABLE graph_source_links ADD CONSTRAINT "
        "graph_source_links_program_id_source_link_id_fkey "
        "FOREIGN KEY(program_id,source_link_id) REFERENCES source_links(program_id,id) "
        "DEFERRABLE INITIALLY IMMEDIATE"
    )
    op.execute("DROP INDEX graph_relations_state_revision_idx")
    op.execute("DROP INDEX graph_entities_state_revision_idx")
