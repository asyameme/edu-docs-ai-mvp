"""Настоящие HTTP, PostgreSQL, Temporal и Neo4j; данные тестов искусственные."""

import asyncio
import os
import time
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from uuid import UUID, uuid4

import httpx
import pytest
from fastapi.testclient import TestClient
from neo4j.exceptions import ConstraintError
from temporalio.client import Client
from temporalio.worker import Replayer, Worker

from app.config import get_settings
from app.domain import SnapshotV2
from app.errors import DomainError
from app.graph_activities import GraphActivities
from app.graph_projection import project_snapshot
from app.graph_repository import GraphRepository
from app.graph_store import GraphStore
from app.graph_workflow import GraphWorkflow
from app.main import app
from app.review_repository import ReviewRepository
from app.review_storage import insert_version
from tests.test_graph_projection import example

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        os.getenv("RUN_INTEGRATION") != "1", reason="Needs isolated Neo4j/PostgreSQL/Temporal"
    ),
]


@pytest.fixture
def http():
    with httpx.Client(base_url="http://frontend", timeout=30, trust_env=False) as client:
        yield client


@pytest.fixture
def store():
    graph = GraphStore(get_settings())
    yield graph
    graph.close()


def post(http, path, body=None, expected=201, key=None):
    response = http.post(path, json=body, headers={"Idempotency-Key": key or uuid4().hex})
    assert response.status_code == expected, response.text
    return response.json()


def prepare(http):
    program = post(http, "/api/v1/programs", {"name": "Искусственная программа графа"})
    root = "/api/v1/programs/" + program["id"]
    document = http.post(
        root + "/documents", files={"file": ("source.pdf", b"Artificial graph evidence")}
    )
    assert document.status_code == 201, document.text
    snapshot = example()
    snapshot["schema_version"] = 1
    snapshot["relations"][0].pop("attributes")
    ref = str(uuid4())
    snapshot["document_ids"] = [document.json()["id"]]
    snapshot["source_refs"] = [
        {"id": ref, "document_id": document.json()["id"], "locator": "table:2;cell:B4"}
    ]
    for item in [*snapshot["entities"], *snapshot["relations"]]:
        item["source_ref_ids"] = [ref]
    version = post(
        http,
        root + "/versions",
        {"expected_version_id": program["current_version_id"], "snapshot": snapshot},
    )
    return program, root, version


def wait(http, root, job):
    deadline = time.monotonic() + 50
    while time.monotonic() < deadline:
        response = http.get(root + "/graph-jobs/" + job["id"])
        assert response.status_code == 200, response.text
        state = response.json()
        if state["status"] in {"completed", "failed"}:
            assert state["status"] == "completed", state
            return state
        time.sleep(0.15)
    pytest.fail("Графовая задача не завершилась")


def build(http, root, version_id, key=None):
    job = post(http, root + "/versions/" + version_id + "/graph/builds", expected=202, key=key)
    return wait(http, root, job)


def test_graph_api_reads_real_nodes_edges_sources_and_isolated_versions(http, store):
    program, root, version = prepare(http)
    job = build(http, root, version["id"])
    base = root + "/versions/" + version["id"] + "/graph"
    assert http.get(base).json()["snapshot_hash"] == job["report"]["manifest"]["snapshot_hash"]
    entities = http.get(base + "/entities").json()
    assert entities["total"] == 2
    first, second = version["snapshot"]["entities"]
    got = http.get(base + "/entities/" + first["id"]).json()
    assert got["entity"] == first
    assert got["sources"] == version["snapshot"]["source_refs"]
    edge = version["snapshot"]["relations"][0]
    assert http.get(base + "/relations/" + edge["id"]).json()["relation"] == edge
    assert http.get(base + "/entities/" + first["id"] + "/neighbors").json()["items"] == [second]
    assert http.get(base + "/entities/" + second["id"] + "/neighbors").json()["total"] == 0
    assert http.get(
        base + "/entities/" + second["id"] + "/neighbors", params={"direction": "in"}
    ).json()["items"] == [first]
    assert (
        http.get(base + "/entities", params={"limit": 1, "offset": 1}).json()["items"]
        == entities["items"][1:]
    )
    assert (
        http.get(base + "/entities/" + first["id"] + "/neighbors", params={"depth": 4}).status_code
        == 422
    )
    changed = deepcopy(version["snapshot"])
    changed["entities"][0]["attributes"]["credits"] = "4.000000000000000001"
    latest = post(
        http, root + "/versions", {"expected_version_id": version["id"], "snapshot": changed}
    )
    assert http.get(root + "/versions/" + latest["id"] + "/graph").status_code == 409
    build(http, root, latest["id"])
    assert http.get(base + "/entities/" + first["id"]).json()["entity"] == first
    assert (
        http.get(root + "/versions/" + latest["id"] + "/graph/entities/" + first["id"])
        .json()["entity"]["attributes"]["credits"]
        .startswith("4.")
    )
    other = post(http, "/api/v1/programs", {"name": "Другая программа"})
    assert (
        http.get(
            "/api/v1/programs/" + other["id"] + "/versions/" + version["id"] + "/graph"
        ).status_code
        == 404
    )
    with store.driver.session(database=store.database) as session:
        count = session.run(
            "MATCH (:Entity {program_id:$p,version_id:$v})-[r:FORMS_COMPETENCY]->() "
            "RETURN count(r) AS n",
            p=program["id"],
            v=version["id"],
        ).single()["n"]
        assert count == 1


def test_concurrent_repeated_builds_do_not_duplicate_or_change_sql_version(http):
    _, root, version = prepare(http)
    path = root + "/versions/" + version["id"] + "/graph/builds"
    key = uuid4().hex
    first = post(http, path, expected=202, key=key)
    assert post(http, path, expected=202, key=key)["id"] == first["id"]
    with ThreadPoolExecutor(max_workers=2) as executor:
        jobs = list(executor.map(lambda _: post(http, path, expected=202), range(2)))
    for job in [first, *jobs]:
        wait(http, root, job)
    assert http.get(root).json()["current_version_id"] == version["id"]
    assert http.get(path.removesuffix("/builds")).json()["node_count"] == 2
    assert len(http.get(root + "/versions").json()) == 2
    post(
        http,
        root + "/versions",
        {"expected_version_id": version["id"], "snapshot": version["snapshot"]},
        expected=409,
        key=key,
    )


def test_atomic_rollback_preserves_previous_graph_on_bad_publication(http, store):
    program, root, version = prepare(http)
    build(http, root, version["id"])
    graph = project_snapshot(program["id"], version["id"], version["snapshot"])
    malformed = project_snapshot(program["id"], version["id"], version["snapshot"])
    malformed["edges"][0]["target_id"] = str(uuid4())
    with pytest.raises(DomainError):
        store.publish(malformed)
    assert store.read(graph)["entities"]
    assert http.get(root + "/versions/" + version["id"] + "/graph").status_code == 200


def test_corrupt_graph_is_refused_and_repaired_from_unchanged_sql(http, store):
    program, root, version = prepare(http)
    build(http, root, version["id"])
    with store.driver.session(database=store.database) as session:
        session.run(
            "MATCH (n:Entity {program_id:$p,version_id:$v}) SET n.attr_code='corrupted'",
            p=program["id"],
            v=version["id"],
        ).consume()
    base = root + "/versions/" + version["id"] + "/graph"
    assert http.get(base).status_code == 503
    build(http, root, version["id"])
    assert http.get(base).status_code == 200
    assert http.get(root + "/versions/" + version["id"]).json() == version


def test_v2_matrix_zero_and_field_evidence_survive_native_graph(http, store):
    program, root, version = prepare(http)
    raw = version["snapshot"]
    raw["schema_version"] = 2
    raw["relations"][0]["attributes"] = {"matrix_value": 0}
    raw["source_refs"][0]["id"] = str(uuid4())
    ref = raw["source_refs"][0]["id"]
    for item in [*raw["entities"], *raw["relations"]]:
        item["source_ref_ids"] = [ref]
    raw["source_links"] = [
        {
            "id": str(uuid4()),
            "target_kind": "relation",
            "target_id": raw["relations"][0]["id"],
            "field_path": "/attributes/matrix_value",
            "source_ref_id": ref,
            "observed_value": {"value": 0},
        }
    ]
    with TestClient(app):
        pool = app.state.resources.postgres
        with pool.connection() as connection:
            from psycopg.rows import dict_row

            with connection.cursor(row_factory=dict_row) as cur:
                ReviewRepository.lock(cur, UUID(program["id"]))
                new = insert_version(
                    cur,
                    UUID(program["id"]),
                    UUID(version["id"]),
                    uuid4(),
                    SnapshotV2.model_validate(raw),
                )
    build(http, root, str(new["id"]))
    base = root + "/versions/" + str(new["id"]) + "/graph"
    path = base + "/entities/" + raw["entities"][0]["id"] + "/neighbors"
    assert http.get(path).json()["total"] == 0
    assert http.get(path, params={"include_zero": "true"}).json()["total"] == 1
    evidence = http.get(base + "/relations/" + raw["relations"][0]["id"]).json()
    assert evidence["observations"] == raw["source_links"]
    assert evidence["relation"]["attributes"]["matrix_value"] == 0
    with store.driver.session(database=store.database) as session:
        session.run(
            "MATCH ()-[r:FORMS_COMPETENCY {program_id:$p,version_id:$v}]->() "
            "SET r.matrix_value=false",
            p=program["id"],
            v=str(new["id"]),
        ).consume()
    assert http.get(base).status_code == 503
    build(http, root, str(new["id"]))
    assert http.get(base).status_code == 200


def test_automatic_reconciliation_restores_lost_graph_without_user_build(http, store):
    program, root, version = prepare(http)
    build(http, root, version["id"])
    with store.driver.session(database=store.database) as session:
        session.run(
            "MATCH (n {program_id:$p,version_id:$v}) DETACH DELETE n",
            p=program["id"],
            v=version["id"],
        ).consume()
    with TestClient(app):
        repo = GraphRepository(app.state.resources.postgres)
        repo.settings = repo.settings.model_copy(update={"graph_auto_build": True})
        deadline = time.monotonic() + 50
        while time.monotonic() < deadline:
            repo.reconcile(store)
            if http.get(root + "/versions/" + version["id"] + "/graph").status_code == 200:
                break
            time.sleep(0.2)
        else:
            pytest.fail("Автоматическая сверка не восстановила граф")


def test_graph_workflow_history_is_replayable(http):
    _, root, version = prepare(http)
    job = build(http, root, version["id"])

    async def replay():
        settings = get_settings()
        client = await Client.connect(
            settings.temporal_address, namespace=settings.temporal_namespace
        )
        handle = client.get_workflow_handle("graph-" + job["id"])
        await handle.result()
        await Replayer(workflows=[GraphWorkflow]).replay_workflow(await handle.fetch_history())

    asyncio.run(replay())


def test_sql_checkpoint_before_completed_workflow_is_reconciled(http):
    program, root, version = prepare(http)
    job = build(http, root, version["id"])
    with TestClient(app):
        with app.state.resources.postgres.connection() as connection:
            connection.execute(
                "UPDATE processing_jobs SET report='{}',output_version_id=NULL WHERE id=%s",
                (UUID(job["id"]),),
            )
    recovered = wait(http, root, job)
    assert recovered["report"].get("outcome") == "completed"
    assert recovered["output_version_id"] == version["id"]
    assert http.get(root + "/versions/" + version["id"]).json() == version


def test_native_uniqueness_and_unexpected_nonentity_edge_are_guarded(http, store):
    program, root, version = prepare(http)
    build(http, root, version["id"])
    first = version["snapshot"]["entities"][0]["id"]
    scope = {"p": program["id"], "v": version["id"], "id": first}
    with store.driver.session(database=store.database) as session:
        with pytest.raises(ConstraintError):
            session.run(
                "CREATE (:Entity {program_id:$p,version_id:$v,entity_id:$id})", **scope
            ).consume()
    probe = uuid4().hex
    try:
        with store.driver.session(database=store.database) as session:
            session.run(
                "CREATE (b:ExternalProbe {test_id:$probe}) WITH b "
                "MATCH (a:Entity {program_id:$p,version_id:$v,entity_id:$id}) "
                "CREATE (a)-[:UNEXPECTED_TEST]->(b)",
                **scope,
                probe=probe,
            ).consume()
        base = root + "/versions/" + version["id"] + "/graph"
        assert http.get(base).status_code == 503
        build(http, root, version["id"])
        assert http.get(base).status_code == 200
    finally:
        with store.driver.session(database=store.database) as session:
            session.run(
                "MATCH (n:ExternalProbe {test_id:$probe}) DETACH DELETE n", probe=probe
            ).consume()


@pytest.mark.skipif(
    os.getenv("GRAPH_RETRY_FAULT_TEST") != "1", reason="Run separately with regular Worker stopped"
)
def test_retry_after_neo4j_commit_before_sql_report_is_safe(http, store):
    program, _, version = prepare(http)

    async def run(pool):
        settings = get_settings()
        repo = GraphRepository(pool)
        job_id = repo.create(UUID(program["id"]), UUID(version["id"]), uuid4().hex)
        activities = GraphActivities(pool, store)
        original = activities.repo.finish
        attempts = []

        def lose_once(job, graph):
            attempts.append(len(attempts) + 1)
            if len(attempts) == 1:
                raise ConnectionError("Искусственный сбой после commit Neo4j до отчёта SQL")
            original(job, graph)

        activities.repo.finish = lose_once
        client = await Client.connect(
            settings.temporal_address, namespace=settings.temporal_namespace
        )
        queue = "graph-fault-" + uuid4().hex
        from concurrent.futures import ThreadPoolExecutor

        with ThreadPoolExecutor(max_workers=2) as executor:
            async with Worker(
                client,
                task_queue=queue,
                workflows=[GraphWorkflow],
                activities=[activities.build, activities.failed],
                activity_executor=executor,
            ):
                result = await client.execute_workflow(
                    GraphWorkflow.run,
                    {"job_id": str(job_id)},
                    id="graph-" + str(job_id),
                    task_queue=queue,
                )
        assert result == "completed" and attempts == [1, 2]
        assert repo.job(UUID(program["id"]), job_id)["report"]["outcome"] == "completed"
        store.read(repo.projection(UUID(program["id"]), UUID(version["id"])))

    with TestClient(app):
        asyncio.run(run(app.state.resources.postgres))
