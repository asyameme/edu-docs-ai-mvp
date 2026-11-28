"""Оптимизация не меняет контракт графа и не ослабляет связи свидетельств."""

import hashlib
import importlib.util
import os
from pathlib import Path
from uuid import uuid4

import psycopg
import pytest
import sqlalchemy as sa
from alembic.migration import MigrationContext
from alembic.operations import Operations
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from app.domain import SnapshotV2
from app.graph_storage import load_graph, parsed_artifact, save_graph

pytestmark = pytest.mark.skipif(
    not os.getenv("GRAPH_TEST_DSN"), reason="Нужна отдельная PostgreSQL для графа"
)


@pytest.fixture
def graph_connection():
    with psycopg.connect(os.environ["GRAPH_TEST_DSN"], row_factory=dict_row) as conn:
        with conn.transaction(force_rollback=True):
            yield conn


def make_graph(conn, schema=2):
    program, document, first, second, relation = [uuid4() for _ in range(5)]
    refs = [uuid4(), uuid4()]
    conn.execute("INSERT INTO programs(id,name) VALUES(%s,'Проверка графа')", (program,))
    conn.execute(
        "INSERT INTO source_documents(id,program_id,original_name,format,media_type,"
        "size_bytes,sha256,content) VALUES(%s,%s,'a.docx','docx','application/octet-stream',"
        "1,%s,%s)",
        (document, program, hashlib.sha256(b"x").hexdigest(), b"x"),
    )
    snapshot = SnapshotV2.model_validate(
        {
            "document_ids": [str(document)],
            "metadata": {"label": "Ё", "nested": {"value": False}},
            "entities": [
                {
                    "id": str(first),
                    "kind": "discipline",
                    "attributes": {"value": False},
                    "source_ref_ids": list(map(str, refs)),
                },
                {"id": str(second), "kind": "competency", "attributes": {"value": 0}},
            ],
            "relations": [
                {
                    "id": str(relation),
                    "source_id": str(first),
                    "target_id": str(second),
                    "type": "FORMS_COMPETENCY",
                    "confidence": 1.0,
                    "attributes": {"matrix_value": 0, "value": None},
                    "source_ref_ids": list(map(str, reversed(refs))),
                }
            ],
            "source_refs": [
                {"id": str(ref), "document_id": str(document), "locator": f"page:{index + 1}"}
                for index, ref in enumerate(reversed(refs))
            ],
            "source_links": [
                {
                    "id": str(uuid4()),
                    "target_kind": kind,
                    "target_id": str(target),
                    "field_path": "/attributes/value",
                    "source_ref_id": str(ref),
                    "observed_value": {"value": value},
                }
                for kind, target, ref, value in (
                    ("relation", relation, refs[1], None),
                    ("entity", first, refs[0], False),
                    ("entity", second, refs[1], 0),
                )
            ],
        }
    ).model_dump(mode="json")
    if schema == 1:
        snapshot["schema_version"] = 1
        snapshot.pop("review")
        snapshot.pop("source_links")
        for item in snapshot["relations"]:
            item.pop("attributes")
        for item in snapshot["source_refs"]:
            item.pop("artifact_id")
            item.pop("artifact_locator")
    with conn.cursor() as cur:
        state = save_graph(cur, program, snapshot)
    return program, state, snapshot


@pytest.mark.parametrize("table", ["graph_entities", "graph_relations"])
def test_state_revision_lookup_has_index(graph_connection, table):
    if graph_connection.execute(
        "SELECT to_regclass(%s) AS name", (table + "_legacy",)
    ).fetchone()["name"]:
        table += "_legacy"
    indexes = graph_connection.execute(
        "SELECT indexdef FROM pg_indexes WHERE schemaname='public' AND tablename=%s", (table,)
    ).fetchall()
    assert any("(graph_state_id, revision_id)" in row["indexdef"] for row in indexes)


def test_only_composite_evidence_identity_fks_remain(graph_connection):
    constraints = graph_connection.execute(
        "SELECT cardinality(conkey) AS n FROM pg_constraint "
        "WHERE conrelid=COALESCE(to_regclass('graph_source_links_legacy'),"
        "'graph_source_links'::regclass) "
        "AND confrelid='source_links'::regclass AND contype='f'"
    ).fetchall()
    assert sorted(row["n"] for row in constraints) == [4, 4]


@pytest.mark.parametrize("schema", [1, 2])
def test_snapshot_order_types_and_empty_members_are_preserved(graph_connection, schema):
    _, state, snapshot = make_graph(graph_connection, schema)
    with graph_connection.cursor() as cur:
        loaded = load_graph(cur, state, snapshot.get("review"))
        assert loaded == snapshot
        if schema == 2:
            assert loaded["source_links"][0]["observed_value"]["value"] is None
            assert loaded["source_links"][1]["observed_value"]["value"] is False
            assert type(loaded["source_links"][2]["observed_value"]["value"]) is int
            review = {"status": "checked", "nested": {"value": False}}
            assert load_graph(cur, state, review) == {**snapshot, "review": review}
    assert (
        graph_connection.execute("SELECT graph_snapshot(%s) AS snapshot", (uuid4(),)).fetchone()[
            "snapshot"
        ]
        is None
    )


def test_pregraph_snapshot_preserves_review_without_a_graph_state(graph_connection):
    review = {"graph_ready": False, "decisions": [], "confirmation": None}
    loaded = graph_connection.execute(
        "SELECT graph_snapshot(NULL,%s) AS snapshot", (Jsonb(review),)
    ).fetchone()["snapshot"]
    assert loaded == {**SnapshotV2().model_dump(mode="json"), "review": review}


def test_snapshot_restores_selected_parsed_artifact(graph_connection):
    conn = graph_connection
    program, state, snapshot = make_graph(conn)
    version, job = uuid4(), uuid4()
    conn.execute(
        "INSERT INTO program_versions(id,program_id,number,graph_state_id,origin) "
        "VALUES(%s,%s,1,%s,'initial')",
        (version, program, state),
    )
    conn.execute(
        "INSERT INTO processing_jobs(id,program_id,input_version_id,type,operation_key,"
        "request_hash,temporal_namespace,workflow_id) "
        "VALUES(%s,%s,%s,'import','artifact-test','test','test','test')",
        (job, program, version),
    )
    document = snapshot["document_ids"][0]
    parsed = {"meta": {"parser_version": "test"}, "value": [0, False, None, "Ё"]}
    with conn.cursor() as cur:
        parsed_artifact(cur, program, document, parsed, job, 1)
        snapshot["metadata"]["parsed_documents"] = {document: parsed}
        selected = save_graph(cur, program, snapshot)
        assert load_graph(cur, selected) == snapshot


def test_state_guard_still_requires_all_revision_sources(graph_connection):
    conn = graph_connection
    program, state, _ = make_graph(conn)
    building = uuid4()
    conn.execute("INSERT INTO graph_states(id,program_id) VALUES(%s,%s)", (building, program))
    conn.execute(
        "INSERT INTO graph_entities SELECT program_id,%s,entity_id,revision_id,position "
        "FROM graph_entities WHERE graph_state_id=%s",
        (building, state),
    )
    with pytest.raises(psycopg.errors.CheckViolation), conn.transaction():
        conn.execute(
            "UPDATE graph_states SET status='sealed',sealed_at=now(),content_hash=%s WHERE id=%s",
            ("a" * 64, building),
        )
    with pytest.raises(psycopg.errors.CheckViolation), conn.transaction():
        conn.execute("DELETE FROM graph_entities WHERE graph_state_id=%s", (state,))


@pytest.mark.parametrize("target_kind", ["entity", "relation"])
@pytest.mark.parametrize(
    "invalid", ["missing_fact", "wrong_target", "wrong_source", "wrong_program"]
)
@pytest.mark.parametrize("deferred", [False, True])
def test_evidence_fks_still_reject_invalid_membership(
    graph_connection, target_kind, invalid, deferred
):
    conn = graph_connection
    program, state, _ = make_graph(conn)
    target_column = "entity_id" if target_kind == "entity" else "relation_id"
    row = conn.execute(
        f"SELECT * FROM graph_source_links WHERE graph_state_id=%s AND {target_column} IS NOT NULL",
        (state,),
    ).fetchone()
    building = uuid4()
    conn.execute("INSERT INTO graph_states(id,program_id) VALUES(%s,%s)", (building, program))
    for table in ("graph_entities", "graph_relations", "graph_documents", "graph_sources"):
        conn.execute(
            f"INSERT INTO {table} SELECT program_id,%s,"
            + {
                "graph_entities": "entity_id,revision_id,position",
                "graph_relations": (
                    "relation_id,revision_id,source_entity_id,target_entity_id,position"
                ),
                "graph_documents": (
                    "document_id,parsed_artifact_id,parsed_artifact_kind,member_name,position"
                ),
                "graph_sources": "source_ref_id,document_id,position",
            }[table]
            + f" FROM {table} WHERE graph_state_id=%s",
            (building, state),
        )
    if invalid == "missing_fact":
        row["source_link_id"] = uuid4()
    elif invalid == "wrong_target":
        row[target_column] = uuid4()
    elif invalid == "wrong_source":
        row["source_ref_id"] = conn.execute(
            "SELECT source_ref_id FROM graph_sources WHERE graph_state_id=%s AND source_ref_id<>%s",
            (state, row["source_ref_id"]),
        ).fetchone()["source_ref_id"]
    else:
        _, other_state, _ = make_graph(conn)
        row["source_link_id"] = conn.execute(
            "SELECT source_link_id FROM graph_source_links WHERE graph_state_id=%s LIMIT 1",
            (other_state,),
        ).fetchone()["source_link_id"]
    with pytest.raises(psycopg.errors.ForeignKeyViolation), conn.transaction():
        if deferred:
            conn.execute("SET CONSTRAINTS ALL DEFERRED")
        conn.execute(
            "INSERT INTO graph_source_links(program_id,graph_state_id,source_link_id,entity_id,"
            "relation_id,source_ref_id,position,public_id) VALUES(%s,%s,%s,%s,%s,%s,0,%s)",
            (
                row["program_id"],
                building,
                row["source_link_id"],
                row["entity_id"],
                row["relation_id"],
                row["source_ref_id"],
                uuid4(),
            ),
        )
        conn.execute("SET CONSTRAINTS ALL IMMEDIATE")


@pytest.mark.parametrize("both_present", [False, True])
def test_evidence_requires_exactly_one_target(graph_connection, both_present):
    conn = graph_connection
    program, state, _ = make_graph(conn)
    building = uuid4()
    row = conn.execute(
        "SELECT * FROM graph_source_links WHERE graph_state_id=%s LIMIT 1", (state,)
    ).fetchone()
    conn.execute("INSERT INTO graph_states(id,program_id) VALUES(%s,%s)", (building, program))
    with pytest.raises(psycopg.errors.CheckViolation), conn.transaction():
        conn.execute(
            "INSERT INTO graph_source_links(program_id,graph_state_id,source_link_id,entity_id,"
            "relation_id,source_ref_id,position,public_id) VALUES(%s,%s,%s,%s,%s,%s,0,%s)",
            (
                program,
                building,
                row["source_link_id"],
                uuid4() if both_present else None,
                uuid4() if both_present else None,
                row["source_ref_id"],
                uuid4(),
            ),
        )


def test_performance_migration_roundtrip_preserves_existing_states():
    dsn = os.getenv("GRAPH_REVISION_MIGRATION_DSN") or os.getenv("GRAPH_MIGRATION_TEST_DSN")
    if not dsn:
        pytest.skip("Нужна отдельная пустая БД для проверки отката миграции 0009")
    dsn = dsn.replace("postgresql+psycopg://", "postgresql://")
    engine = sa.create_engine(
        "postgresql+psycopg://", creator=lambda: psycopg.connect(dsn)
    )
    migration = Path(__file__).parents[1] / "migrations/versions/0009_graph_performance.py"
    spec = importlib.util.spec_from_file_location("graph_performance_migration", migration)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    try:
        with engine.connect() as conn, conn.begin():
            operations = Operations(MigrationContext.configure(conn))
            for file in sorted(migration.parent.glob("000[1-8]_*.py")):
                initial_spec = importlib.util.spec_from_file_location(file.stem, file)
                initial_module = importlib.util.module_from_spec(initial_spec)
                initial_spec.loader.exec_module(initial_module)
                initial_module.op = operations
                initial_module.upgrade()
            module.op = operations
            module.upgrade()
            raw = conn.connection.driver_connection
            raw.row_factory = dict_row
            module.op = Operations(MigrationContext.configure(conn))
            states = [make_graph(raw, schema) for schema in (1, 2)]
            hashes = raw.execute("SELECT id,content_hash FROM graph_states ORDER BY id").fetchall()
            # Проверяем 0009 на её собственной схеме, до преобразования таблиц в 0010.
            module.downgrade()
            for _, state, snapshot in states:
                assert load_graph(raw.cursor(), state, snapshot.get("review")) == snapshot
            module.upgrade()
            for _, state, snapshot in states:
                assert load_graph(raw.cursor(), state, snapshot.get("review")) == snapshot
            assert (
                raw.execute("SELECT id,content_hash FROM graph_states ORDER BY id").fetchall()
                == hashes
            )
            conn.rollback()
    finally:
        engine.dispose()


def test_graph_validation_uses_bounded_set_join_plan(graph_connection):
    config = graph_connection.execute(
        "SELECT proconfig FROM pg_proc WHERE oid='graph_validate_contents(uuid)'::regprocedure"
    ).fetchone()["proconfig"]
    assert "jit=off" in (config or [])
    assert "enable_nestloop=off" in (config or [])
