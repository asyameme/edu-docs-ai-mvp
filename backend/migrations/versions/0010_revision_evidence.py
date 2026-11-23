"""Перенос свидетельств в неизменяемые наборы редакций с проверкой всей истории."""

from pathlib import Path

from alembic import op
from psycopg import sql
from psycopg.rows import dict_row

from app.graph_composition import GraphComposition

revision = "0010_revision_evidence"
down_revision = "0009_graph_performance"
branch_labels = None
depends_on = None

TABLES = (
    "graph_entities",
    "graph_relations",
    "graph_documents",
    "graph_sources",
    "graph_source_links",
)
NEW_TABLES = (
    "graph_document_selections",
    "revision_evidence_sets",
    "graph_evidence_orders",
    "graph_state_contents",
    "sealed_entity_revisions",
    "sealed_relation_revisions",
    "graph_snapshot_cache",
    "graph_version_snapshots",
)


def upgrade():
    raw = op.get_bind().connection.driver_connection
    folder = Path(__file__).parents[1] / "sql"
    with raw.cursor(row_factory=dict_row) as cur:
        cur.execute(
            "LOCK TABLE graph_states,graph_entities,graph_relations,graph_documents,"
            "graph_sources,graph_source_links,entity_revisions,relation_revisions,"
            "revision_sources,source_links IN ACCESS EXCLUSIVE MODE"
        )
        cur.execute(
            "CREATE TEMP TABLE graph_evidence_migration_check ON COMMIT DROP AS "
            "SELECT id,content_hash,encode(sha256(convert_to(graph_snapshot(id)::text,'UTF8')),"
            "'hex') AS snapshot_hash FROM graph_states WHERE status='sealed'"
        )
        cur.execute((folder / "revision_evidence.sql").read_text(encoding="utf-8"))
        states = cur.execute(
            "SELECT id,program_id FROM graph_states WHERE status='sealed'"
        ).fetchall()
        for state in states:
            composition = GraphComposition(cur, state["program_id"])
            for table in TABLES:
                columns = (
                    "*"
                    if table != "graph_documents"
                    else (
                        "program_id,graph_state_id,document_id,parsed_artifact_id,member_name,position"
                    )
                )
                rows = cur.execute(
                    sql.SQL(
                        "SELECT " + columns + " FROM {} WHERE graph_state_id=%s ORDER BY position"
                    ).format(sql.Identifier(table)),
                    (state["id"],),
                ).fetchall()
                composition.add(table, [tuple(row.values()) for row in rows])
            composition.persist(state["id"], cache=False)
            cur.execute("SELECT graph_validate_contents(%s)", (state["id"],))
        for table in TABLES:
            cur.execute(
                sql.SQL("ALTER TABLE {} RENAME TO {}").format(
                    sql.Identifier(table), sql.Identifier(table + "_legacy")
                )
            )
        cur.execute((folder / "graph_composition_views.sql").read_text(encoding="utf-8"))
        cur.execute(
            "INSERT INTO sealed_entity_revisions SELECT DISTINCT g.revision_id "
            "FROM graph_entities g JOIN graph_states s ON s.id=g.graph_state_id "
            "WHERE s.status='sealed'"
        )
        cur.execute(
            "INSERT INTO sealed_relation_revisions SELECT DISTINCT g.revision_id "
            "FROM graph_relations g JOIN graph_states s ON s.id=g.graph_state_id "
            "WHERE s.status='sealed'"
        )
        for table in NEW_TABLES:
            cur.execute(
                sql.SQL(
                    "CREATE TRIGGER immutable_row BEFORE UPDATE OR DELETE ON {} "
                    "FOR EACH ROW EXECUTE FUNCTION graph_reject_mutation()"
                ).format(sql.Identifier(table))
            )
            cur.execute(
                sql.SQL(
                    "CREATE TRIGGER no_truncate BEFORE TRUNCATE ON {} "
                    "FOR EACH STATEMENT EXECUTE FUNCTION graph_reject_mutation()"
                ).format(sql.Identifier(table))
            )
        cur.execute(
            "CREATE TRIGGER check_evidence_sets AFTER INSERT ON revision_evidence_sets "
            "REFERENCING NEW TABLE AS inserted_evidence_sets "
            "FOR EACH STATEMENT EXECUTE FUNCTION graph_check_evidence_sets()"
        )
        cur.execute(
            "CREATE TRIGGER guard_evidence_order BEFORE INSERT ON graph_evidence_orders "
            "FOR EACH ROW EXECUTE FUNCTION graph_guard_evidence_order()"
        )
        cur.execute(
            "CREATE TRIGGER guard_contents BEFORE INSERT ON graph_state_contents "
            "FOR EACH ROW EXECUTE FUNCTION graph_guard_contents()"
        )
        cur.execute(
            "CREATE TRIGGER check_contents AFTER INSERT ON graph_state_contents "
            "FOR EACH ROW EXECUTE FUNCTION graph_check_contents()"
        )
        for table in ("sealed_entity_revisions", "sealed_relation_revisions"):
            cur.execute(
                sql.SQL(
                    "CREATE TRIGGER check_revision_seals AFTER INSERT ON {} "
                    "REFERENCING NEW TABLE AS inserted_sealed_revisions "
                    "FOR EACH STATEMENT EXECUTE FUNCTION graph_check_revision_seals()"
                ).format(sql.Identifier(table))
            )
        builder = (folder / "graph_snapshot_optimized.sql").read_text(encoding="utf-8")
        cur.execute(
            builder.replace(
                "FUNCTION graph_snapshot_with_graph(", "FUNCTION graph_snapshot_legacy_graph(", 1
            )
        )
        cur.execute((folder / "graph_compact_snapshot.sql").read_text(encoding="utf-8"))
        cur.execute((folder / "graph_snapshot_cache.sql").read_text(encoding="utf-8"))
        cur.execute(
            "INSERT INTO graph_snapshot_cache(graph_state_id) SELECT id FROM graph_states "
            "WHERE status='sealed'"
        )
        cur.execute(
            "INSERT INTO graph_version_snapshots(graph_state_id,review) "
            "SELECT DISTINCT v.graph_state_id,v.review FROM program_versions v "
            "JOIN graph_states s ON s.id=v.graph_state_id "
            "WHERE v.review IS NOT NULL AND s.schema_version=2"
        )
        cur.execute((folder / "graph_snapshot_wrapper.sql").read_text(encoding="utf-8"))
        changed = cur.execute(
            "SELECT old.id FROM graph_evidence_migration_check old "
            "JOIN graph_states s ON s.id=old.id "
            "WHERE s.content_hash IS DISTINCT FROM old.content_hash OR "
            "encode(sha256(convert_to(graph_snapshot(s.id)::text,'UTF8')),'hex')<>old.snapshot_hash"
        ).fetchone()
        if changed:
            raise ValueError(f"История {changed['id']} изменилась; миграция отменена")
        cur.execute("DROP TABLE graph_evidence_migration_check")


def downgrade():
    raise RuntimeError(
        "Откат новой схемы требует проверенной резервной копии; история не удаляется"
    )
