"""Редакции и наборы свидетельств переиспользуются без потери истории."""

import importlib.util
import os
from copy import deepcopy
from pathlib import Path
from uuid import UUID, uuid4, uuid5

import psycopg
import pytest
import sqlalchemy as sa
from alembic.migration import MigrationContext
from alembic.operations import Operations
from psycopg import sql
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb
from test_graph_performance import make_graph

from app.errors import DomainError
from app.graph_storage import load_graph, save_graph
from app.review_logic import digest

pytestmark = pytest.mark.skipif(
    not os.getenv("GRAPH_TEST_DSN"), reason="Нужна отдельная PostgreSQL для графа"
)


@pytest.fixture
def connection():
    with psycopg.connect(os.environ["GRAPH_TEST_DSN"], row_factory=dict_row) as conn:
        with conn.transaction(force_rollback=True):
            yield conn


def write(conn, program, snapshot):
    with conn.cursor() as cur:
        return save_graph(cur, program, snapshot)


def test_single_edit_reuses_unmodified_evidence_and_writes_no_full_memberships(connection):
    conn = connection
    program, before, snapshot = make_graph(conn)
    first = conn.execute(
        "SELECT * FROM graph_state_contents WHERE graph_state_id=%s", (before,)
    ).fetchone()
    changed = deepcopy(snapshot)
    changed["entities"][0]["attributes"]["value"] = "Изменено"
    after = write(conn, program, changed)
    second = conn.execute(
        "SELECT * FROM graph_state_contents WHERE graph_state_id=%s", (after,)
    ).fetchone()
    assert first["entity_revision_ids"][1:] == second["entity_revision_ids"][1:]
    assert first["entity_revision_ids"][0] != second["entity_revision_ids"][0]
    assert first["relation_revision_ids"] == second["relation_revision_ids"]
    assert first["evidence_order_id"] == second["evidence_order_id"]
    assert len(set(second["evidence_set_ids"]) - set(first["evidence_set_ids"])) == 1
    assert conn.execute(
        "SELECT count(*) AS n FROM graph_source_links_legacy WHERE program_id=%s", (program,)
    ).fetchone()["n"] == 0
    with conn.cursor() as cur:
        assert load_graph(cur, before) == snapshot
        assert load_graph(cur, after) == changed


def test_existing_fact_collision_is_still_rejected_and_rolled_back(connection):
    conn = connection
    program, original, snapshot = make_graph(conn)
    changed = deepcopy(snapshot)
    link = changed["source_links"][1]
    link["observed_value"]["value"] = 11
    fact = uuid5(UUID(str(program)), "source-link-fact:" + digest(link))
    conn.execute(
        "INSERT INTO source_links(id,program_id,entity_id,source_ref_id,field_path,observed_value) "
        "VALUES(%s,%s,%s,%s,%s,%s)",
        (
            fact,
            program,
            link["target_id"],
            link["source_ref_id"],
            link["field_path"],
            Jsonb({"value": 999}),
        ),
    )
    before = conn.execute(
        "SELECT count(*) AS n FROM graph_states WHERE program_id=%s", (program,)
    ).fetchone()["n"]
    with pytest.raises(DomainError, match="нового ID"), conn.transaction():
        write(conn, program, changed)
    assert (
        conn.execute(
            "SELECT count(*) AS n FROM graph_states WHERE program_id=%s", (program,)
        ).fetchone()["n"]
        == before
    )
    with conn.cursor() as cur:
        assert load_graph(cur, original) == snapshot


def test_same_revision_can_keep_distinct_historical_evidence_variants(connection):
    conn = connection
    program, before, snapshot = make_graph(conn)
    changed = deepcopy(snapshot)
    changed["source_links"][1]["observed_value"]["value"] = 0
    after = write(conn, program, changed)
    rows = conn.execute(
        "SELECT entity_revision_ids,evidence_set_ids FROM graph_state_contents "
        "WHERE graph_state_id IN (%s,%s)", (before, after)
    ).fetchall()
    assert rows[0]["entity_revision_ids"] == rows[1]["entity_revision_ids"]
    assert rows[0]["evidence_set_ids"] != rows[1]["evidence_set_ids"]
    with conn.cursor() as cur:
        assert load_graph(cur, before)["source_links"][1]["observed_value"]["value"] is False
        value = load_graph(cur, after)["source_links"][1]["observed_value"]["value"]
        assert type(value) is int and value == 0


def test_evidence_global_order_removal_and_empty_sets_are_preserved(connection):
    conn = connection
    program, before, snapshot = make_graph(conn)
    for links in (list(reversed(snapshot["source_links"])), snapshot["source_links"][:1], []):
        changed = {**snapshot, "source_links": links}
        state = write(conn, program, changed)
        with conn.cursor() as cur:
            assert load_graph(cur, state) == changed
            assert load_graph(cur, before) == snapshot


def test_read_projection_is_derived_and_cannot_be_forged_or_changed(connection):
    conn = connection
    program, state, snapshot = make_graph(conn)
    building = uuid4()
    conn.execute(
        "INSERT INTO graph_states(id,program_id,schema_version,metadata) "
        "SELECT %s,program_id,schema_version,metadata FROM graph_states WHERE id=%s",
        (building, state),
    )
    conn.execute(
        "INSERT INTO graph_state_contents SELECT %s,program_id,entity_revision_ids,"
        "relation_revision_ids,document_selection_ids,source_ref_ids,evidence_set_ids,"
        "evidence_order_id FROM graph_state_contents WHERE graph_state_id=%s",
        (building, state),
    )
    conn.execute("INSERT INTO graph_snapshot_cache(graph_state_id,payload) VALUES(%s,%s)",
                 (building,Jsonb({"forged":True})))
    with conn.cursor() as cur:
        assert load_graph(cur,building)==snapshot
    for query in (
        "UPDATE graph_snapshot_cache SET payload='{}'",
        "DELETE FROM graph_snapshot_cache",
        "TRUNCATE graph_snapshot_cache",
    ):
        with pytest.raises(psycopg.errors.CheckViolation), conn.transaction():
            conn.execute(query)
    with pytest.raises(psycopg.errors.CheckViolation),conn.transaction():
        conn.execute("UPDATE graph_states SET metadata='{}' WHERE id=%s",(building,))
    owner = conn.execute(
        "SELECT entity_revision_ids[1] AS id FROM graph_state_contents "
        "WHERE graph_state_id=%s",
        (building,),
    ).fetchone()["id"]
    with pytest.raises(psycopg.errors.CheckViolation), conn.transaction():
        conn.execute("INSERT INTO revision_sources VALUES(%s,%s,NULL,%s,99)",
                     (program,owner,snapshot["source_refs"][0]["id"]))


@pytest.mark.parametrize("table",["sealed_entity_revisions","sealed_relation_revisions"])
def test_revision_freeze_registry_cannot_be_removed(connection,table):
    make_graph(connection)
    for query in (f"DELETE FROM {table}",f"TRUNCATE {table}"):
        with pytest.raises(psycopg.errors.CheckViolation),connection.transaction():
            connection.execute(query)


@pytest.mark.parametrize("bad", ["fact", "owner", "public_ids"])
def test_invalid_revision_evidence_is_rejected(connection, bad):
    conn = connection
    program, state, _ = make_graph(conn)
    row = conn.execute(
        "SELECT e.* FROM revision_evidence_sets e JOIN graph_state_contents c "
        "ON e.id=ANY(c.evidence_set_ids) WHERE c.graph_state_id=%s "
        "AND e.entity_revision_id IS NOT NULL LIMIT 1", (state,)
    ).fetchone()
    row["id"] = uuid4()
    if bad == "fact":
        row["source_link_ids"][0] = uuid4()
    elif bad == "owner":
        row["entity_revision_id"] = uuid4()
    else:
        row["public_ids"] = []
    with pytest.raises(psycopg.IntegrityError), conn.transaction():
        conn.execute(
            "INSERT INTO revision_evidence_sets(id,program_id,entity_revision_id,"
            "relation_revision_id,"
            "source_link_ids,public_ids) VALUES(%s,%s,%s,%s,%s,%s)",
            tuple(row[key] for key in ("id", "program_id", "entity_revision_id",
                                      "relation_revision_id", "source_link_ids", "public_ids")),
        )


@pytest.mark.parametrize("bad", ["entity", "source", "document", "evidence", "order"])
def test_invalid_compact_composition_is_rejected(connection, bad):
    conn = connection
    program, state, _ = make_graph(conn)
    row = conn.execute(
        "SELECT * FROM graph_state_contents WHERE graph_state_id=%s", (state,)
    ).fetchone()
    building = uuid4()
    conn.execute("INSERT INTO graph_states(id,program_id) VALUES(%s,%s)", (building, program))
    row["graph_state_id"] = building
    key = {"entity": "entity_revision_ids", "source": "source_ref_ids",
           "document": "document_selection_ids", "evidence": "evidence_set_ids"}.get(bad)
    if key:
        row[key][0] = uuid4()
    else:
        row["evidence_order_id"] = uuid4()
    with pytest.raises(psycopg.IntegrityError), conn.transaction():
        conn.execute(
            "INSERT INTO graph_state_contents(graph_state_id,program_id,entity_revision_ids,"
            "relation_revision_ids,document_selection_ids,source_ref_ids,evidence_set_ids,"
            "evidence_order_id) VALUES(%s,%s,%s,%s,%s,%s,%s,%s)",
            tuple(row[key] for key in ("graph_state_id", "program_id", "entity_revision_ids",
                  "relation_revision_ids", "document_selection_ids", "source_ref_ids",
                  "evidence_set_ids", "evidence_order_id")),
        )


@pytest.mark.parametrize("table", ["revision_evidence_sets", "graph_evidence_orders",
                                  "graph_document_selections", "graph_state_contents"])
def test_new_history_tables_reject_update_delete_and_truncate(connection, table):
    conn = connection
    make_graph(conn)
    for query in (f"UPDATE {table} SET program_id=program_id", f"DELETE FROM {table}",
                  f"TRUNCATE {table} CASCADE"):
        with pytest.raises(psycopg.errors.CheckViolation), conn.transaction():
            conn.execute(query)


def test_migration_preserves_revision_ids_public_ids_hashes_and_legacy_snapshots():
    dsn = os.getenv("GRAPH_REVISION_MIGRATION_DSN")
    if not dsn:
        pytest.skip("Нужна отдельная пустая БД переноса свидетельств")
    engine = sa.create_engine("postgresql+psycopg://", creator=lambda: psycopg.connect(dsn))
    folder = Path(__file__).parents[1] / "migrations/versions"
    try:
        with engine.connect() as conn, conn.begin():
            operations = Operations(MigrationContext.configure(conn))
            for file in sorted(folder.glob("*.py")):
                if file.name.startswith("0010"):
                    break
                spec = importlib.util.spec_from_file_location(file.stem, file)
                module = importlib.util.module_from_spec(spec)
                spec.loader.exec_module(module)
                module.op = operations
                module.upgrade()
            raw = conn.connection.driver_connection
            raw.row_factory = dict_row
            states = [make_graph(raw, schema) for schema in (1, 2)]
            program, _, snapshot = states[1]
            changed = deepcopy(snapshot)
            changed["source_links"][1]["observed_value"]["value"] = 0
            states.append((program, write(raw, program, changed), changed))
            hashes = raw.execute("SELECT id,content_hash FROM graph_states ORDER BY id").fetchall()
            role = "revision_runtime_" + uuid4().hex
            raw.execute(sql.SQL("CREATE ROLE {} NOLOGIN").format(sql.Identifier(role)))
            raw.execute(sql.SQL("GRANT USAGE ON SCHEMA public TO {}").format(sql.Identifier(role)))
            raw.execute(
                sql.SQL(
                    "GRANT SELECT,INSERT,UPDATE,DELETE ON ALL TABLES "
                    "IN SCHEMA public TO {}"
                ).format(sql.Identifier(role))
            )
            file = folder / "0010_revision_evidence.py"
            spec = importlib.util.spec_from_file_location(file.stem, file)
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
            module.op = operations
            module.upgrade()
            for _, state, snapshot in states:
                with raw.cursor() as cur:
                    assert load_graph(cur, state) == snapshot
            assert (
                raw.execute("SELECT id,content_hash FROM graph_states ORDER BY id").fetchall()
                == hashes
            )
            assert raw.execute(
                "SELECT count(*) AS n FROM graph_state_contents"
            ).fetchone()["n"] == len(states)
            raw.execute(sql.SQL("SET LOCAL ROLE {}").format(sql.Identifier(role)))
            changed = deepcopy(states[1][2])
            changed["entities"][0]["attributes"]["value"] = "Проверка обычной роли"
            saved = write(raw, states[1][0], changed)
            with raw.cursor() as cur:
                assert load_graph(cur, saved) == changed
            with pytest.raises(psycopg.errors.InsufficientPrivilege), raw.transaction():
                raw.execute("ALTER TABLE revision_evidence_sets DISABLE TRIGGER ALL")
            raw.execute("RESET ROLE")
            conn.rollback()
    finally:
        engine.dispose()
