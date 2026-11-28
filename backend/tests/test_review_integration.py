"""Настоящий HTTP/PostgreSQL/Temporal: исходная версия, выбор и проверка."""

import io
import json
import os
import time
import zipfile
from concurrent.futures import ThreadPoolExecutor
from uuid import uuid4

import httpx
import pytest
from docx import Document
from test_generation_docx_patch import make_docx as minimal_docx

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        os.getenv("REVIEW_FIXTURE_TESTS") != "1", reason="Needs artificial review HTTP server"
    ),
]


def wait(http, path):
    deadline = time.monotonic() + 40
    while time.monotonic() < deadline:
        response = http.get(path)
        assert response.status_code == 200, response.text
        job = response.json()
        if job["status"] not in {"pending", "processing"}:
            return job
        time.sleep(0.1)
    pytest.fail("Задача не завершилась")


def post(http, path, body, status, key=None):
    response = http.post(path, json=body, headers={"Idempotency-Key": key or uuid4().hex})
    assert response.status_code == status, response.text
    return response.json()


def make_docx(*body_items):
    """Создать полноценный DOCX для чтения через python-docx и проверки правок."""
    complete = io.BytesIO()
    Document().save(complete)
    with zipfile.ZipFile(io.BytesIO(minimal_docx(*body_items))) as minimal:
        document = minimal.read("word/document.xml")
    result = io.BytesIO()
    with zipfile.ZipFile(complete) as source, zipfile.ZipFile(result, "w") as target:
        for item in source.infolist():
            target.writestr(
                item, document if item.filename == "word/document.xml" else source.read(item)
            )
    return result.getvalue()


def prepare(http, filename="valid.docx", *, wait_analysis=True, content=None):
    program = post(http, "/api/v1/programs", {"name": "Искусственная проверка Backend4"}, 201)
    root = "/api/v1/programs/" + program["id"]
    stream = io.BytesIO()
    with zipfile.ZipFile(stream, "w") as archive:
        archive.writestr(filename, content if content is not None else b"valid " + uuid4().bytes)
    uploaded = http.post(
        root + "/imports",
        files={"file": ("package.zip", stream.getvalue())},
        headers={"Idempotency-Key": uuid4().hex},
    )
    assert uploaded.status_code == 202, uploaded.text
    imported = wait(http, root + "/imports/" + uploaded.json()["id"])
    assert imported["status"] == "completed"
    body = {"import_job_id": imported["id"], "expected_version_id": program["current_version_id"]}
    key = uuid4().hex
    accepted = post(http, root + "/reviews", body, 202, key)
    assert post(http, root + "/reviews", body, 202, key)["id"] == accepted["id"]
    job = wait(http, root + "/reviews/" + accepted["id"]) if wait_analysis else accepted
    return root, imported, job


def choose(http, root, job, variant_index=1):
    details = http.get(root + "/versions/" + job["output_version_id"] + "/review").json()
    issue = next(item for item in details["issues"] if item["requires_decision"])
    body = {
        "expected_version_id": job["output_version_id"],
        "conflict_id": issue["id"],
        "variant_id": issue["variants"][variant_index]["id"],
        "signature": "Тестовый пользователь",
    }
    key = uuid4().hex
    version = post(http, root + "/reviews/" + job["id"] + "/decisions", body, 201, key)
    assert (
        post(http, root + "/reviews/" + job["id"] + "/decisions", body, 201, key)["id"]
        == version["id"]
    )
    return version, issue


def run_check(http, root, job, version_id):
    path = root + "/reviews/" + job["id"] + "/checks"
    accepted = post(http, path, {"version_id": version_id}, 202)
    return wait(http, path + "/" + accepted["id"])


def test_complete_review_preserves_original_sources_versions_and_checked_export():
    with httpx.Client(base_url="http://frontend", timeout=30, trust_env=False) as http:
        original_docx = make_docx("<w:p><w:r><w:t>Тестовая дисциплина: 108 часов</w:t></w:r></w:p>")
        root, imported, job = prepare(http, "РПД/РПД_матанализ1.docx", content=original_docx)
        assert job["status"] == "completed", job
        original = http.get(root + "/versions/" + job["output_version_id"]).json()
        assert original["snapshot"]["entities"]
        assert original["snapshot"]["source_refs"]
        assert original["snapshot"]["review"]["decisions"] == []
        version, issue = choose(http, root, job)
        assert version["snapshot"]["entities"][0]["attributes"]["hours_total"] == 144
        checked = run_check(http, root, job, version["id"])
        assert checked["status"] == "completed", checked
        details = http.get(root + "/versions/" + version["id"] + "/review").json()
        assert details["check_report"]["coverage"][0]["reason"] == "Получено часов: 144"
        assert details["lifecycle"]["can_generate"]
        assert details["confirmation"] is None
        assert (
            next(i for i in details["issues"] if i["id"] == issue["id"])["effective_status"]
            == "resolved"
        )
        assert http.get(root + "/versions/" + original["id"]).json() == original
        source = version["snapshot"]["source_refs"][0]
        downloaded = http.get(root + "/documents/" + source["document_id"] + "/download")
        assert downloaded.content == original_docx
        import psycopg

        from app.config import get_settings
        from app.review_logic import digest

        settings = get_settings()
        with psycopg.connect(
            host=settings.postgres_host,
            dbname=settings.postgres_db,
            user=settings.postgres_user,
            password=settings.postgres_password.get_secret_value(),
        ) as conn:
            rows = conn.execute(
                "SELECT actor_type,actor_id FROM change_log WHERE version_id=%s", (version["id"],)
            ).fetchall()
            assert rows and all(row == ("user", "Тестовый пользователь") for row in rows)
            artifact, config, config_hash = conn.execute(
                "SELECT id,parser_config,parser_config_hash FROM document_artifacts "
                "WHERE producing_job_id=%s AND kind='markdown'",
                (job["id"],),
            ).fetchone()
            assert config_hash == digest(config)
        artifact_response = http.get(
            root
            + "/documents/"
            + source["document_id"]
            + "/artifacts/"
            + str(artifact)
            + "/download"
        )
        assert artifact_response.status_code == 200
        assert artifact_response.text == "# Искусственный Markdown\n"
        generation = post(http, root + "/generation", {"version_id": version["id"]}, 202)
        generated = wait(http, root + "/generation/" + generation["id"])
        assert generated["status"] == "completed", generated
        assert http.get(root + "/generation/" + generation["id"] + "/download").status_code == 200
        assert len(http.get(root + "/versions").json()) == 3
        assert http.get(root + "/imports/" + imported["id"]).json()["status"] == "completed"
        restored = post(
            http,
            root + "/versions/" + version["id"] + "/restore",
            {"expected_version_id": version["id"], "signature": "Тест"},
            201,
        )
        assert restored["snapshot"]["review"]["decisions"]
        restored_details = http.get(root + "/versions/" + restored["id"] + "/review").json()
        assert restored_details["lifecycle"]["check_job_id"]


def test_unresolved_source_relation_keeps_graph_and_selected_decision():
    with httpx.Client(base_url="http://frontend", timeout=30, trust_env=False) as http:
        root, _, job = prepare(http, "РПД/unresolved.docx")
        version, _ = choose(http, root, job)
        assert version["snapshot"]["entities"]
        assert version["snapshot"]["review"]["graph_ready"]
        assert len(version["snapshot"]["review"]["decisions"]) == 1
        assert version["snapshot"]["metadata"]["unresolved_relations"]
        checked = run_check(http, root, job, version["id"])
        assert checked["status"] == "completed", checked
        details = http.get(root + "/versions/" + version["id"] + "/review").json()
        observation = next(
            i for i in details["issues"] if i["rule_code"] == "SOURCE_RELATION_UNRESOLVED"
        )
        assert observation["severity"] == "warning"
        assert observation["source_document_ids"]


def test_repeated_analysis_does_not_replace_saved_source_graph():
    with httpx.Client(base_url="http://frontend", timeout=30, trust_env=False) as http:
        root, imported, job = prepare(http, "noconflict-unresolved.docx")
        source = http.get(root + "/versions/" + job["output_version_id"]).json()
        assert source["snapshot"]["review"]["graph_ready"]
        repeated = post(
            http,
            root + "/reviews",
            {
                "import_job_id": imported["id"],
                "expected_version_id": source["id"],
                "rerun_same_import": True,
            },
            202,
        )
        assert repeated["id"] == job["id"]
        assert http.get(root + "/versions/" + source["id"]).json() == source


@pytest.mark.parametrize("filename", ["warning.docx", "blocking.docx", "skipped.docx"])
def test_export_allows_normative_warnings_but_requires_final_coverage(filename):
    with httpx.Client(base_url="http://frontend", timeout=30, trust_env=False) as http:
        root, _, job = prepare(http, filename)
        version, _ = choose(http, root, job)
        checked = run_check(http, root, job, version["id"])
        details = http.get(root + "/versions/" + version["id"] + "/review").json()
        assert details["confirmation"] is None
        if filename.startswith("skipped"):
            assert not details["lifecycle"]["can_generate"]
            post(http, root + "/generation", {"version_id": version["id"]}, 409)
        else:
            assert checked["report"]["coverage"][0]["status"] == "failed"
            assert checked["report"]["issues"][0]["severity"] == "warning"
            assert details["lifecycle"]["can_generate"]
            post(http, root + "/generation", {"version_id": version["id"]}, 202)


def test_stale_check_and_concurrent_choices_do_not_overwrite_history():
    with httpx.Client(base_url="http://frontend", timeout=30, trust_env=False) as http:
        root, _, job = prepare(http)
        details = http.get(root + "/versions/" + job["output_version_id"] + "/review").json()
        issue = next(item for item in details["issues"] if item["requires_decision"])
        path = root + "/reviews/" + job["id"]
        requests = [
            {
                "expected_version_id": job["output_version_id"],
                "conflict_id": issue["id"],
                "variant_id": variant["id"],
                "signature": "Тестовый пользователь",
            }
            for variant in issue["variants"]
        ]
        with ThreadPoolExecutor(max_workers=2) as executor:
            responses = list(
                executor.map(
                    lambda body: http.post(
                        path + "/decisions", json=body, headers={"Idempotency-Key": uuid4().hex}
                    ),
                    requests,
                )
            )
        assert sorted(response.status_code for response in responses) == [201, 409]
        version = next(response.json() for response in responses if response.status_code == 201)
        checked = run_check(http, root, job, version["id"])
        opposite = next(
            v
            for v in issue["variants"]
            if v["id"] != version["snapshot"]["review"]["decisions"][0]["variant_id"]
        )
        changed = post(
            http,
            path + "/decisions",
            {
                "expected_version_id": version["id"],
                "conflict_id": issue["id"],
                "variant_id": opposite["id"],
                "signature": "Тестовый пользователь",
            },
            201,
        )
        assert (
            http.get(root + "/versions/" + changed["id"] + "/review").json()["lifecycle"][
                "check_job_id"
            ]
            != checked["id"]
        )
        assert (
            changed["snapshot"]["entities"][0]["attributes"]["hours_total"]
            == opposite["changes"][0]["value"]
        )


@pytest.mark.parametrize(
    "filename", ["noconflict.docx", "many.docx", "malformed.docx", "transient-analysis.docx"]
)
def test_analysis_empty_many_invalid_and_retry_results(filename):
    with httpx.Client(base_url="http://frontend", timeout=30, trust_env=False) as http:
        root, _, job = prepare(http, filename)
        if filename.startswith("malformed"):
            assert job["status"] == "failed"
            assert len(http.get(root + "/versions").json()) == 2
            return
        assert job["status"] == "completed", job
        details = http.get(
            root + "/versions/" + job["output_version_id"] + "/review", params={"limit": 2}
        ).json()
        expected = (
            0 if filename.startswith("noconflict") else (19 if filename.startswith("many") else 1)
        )
        assert details["conflicts_count"] == expected
        assert len(details["issues"]) == min(2, details["issues_count"])
        assert sum(item["requires_decision"] for item in details["issues"]) <= expected
        if expected == 0:
            checked = run_check(http, root, job, job["output_version_id"])
            assert checked["status"] == "completed"


def test_unresolved_conflict_foreign_ids_and_shared_operation_keys_are_rejected():
    with httpx.Client(base_url="http://frontend", timeout=30, trust_env=False) as http:
        root, imported, job = prepare(http)
        path = root + "/reviews/" + job["id"]
        post(
            http,
            path + "/checks",
            {"version_id": job["output_version_id"]},
            202,
        )
        details = http.get(root + "/versions/" + job["output_version_id"] + "/review").json()
        issue = next(item for item in details["issues"] if item["requires_decision"])
        post(
            http,
            path + "/decisions",
            {
                "expected_version_id": job["output_version_id"],
                "conflict_id": issue["id"],
                "variant_id": str(uuid4()),
                "signature": "Тестовый пользователь",
            },
            422,
        )
        other = post(http, "/api/v1/programs", {"name": "Другая программа"}, 201)
        assert (
            http.get("/api/v1/programs/" + other["id"] + "/reviews/" + job["id"]).status_code == 404
        )
        assert (
            http.get(
                "/api/v1/programs/"
                + other["id"]
                + "/versions/"
                + job["output_version_id"]
                + "/review"
            ).status_code
            == 404
        )
        decision_key = uuid4().hex
        version = post(
            http,
            path + "/decisions",
            {
                "expected_version_id": job["output_version_id"],
                "conflict_id": issue["id"],
                "variant_id": issue["variants"][0]["id"],
                "signature": "Тестовый пользователь",
            },
            201,
            decision_key,
        )
        post(
            http,
            root + "/reviews",
            {"import_job_id": imported["id"], "expected_version_id": version["id"]},
            409,
            decision_key,
        )


def test_source_graph_is_available_before_choices_and_confirmation_endpoint_is_absent():
    with httpx.Client(base_url="http://frontend", timeout=30, trust_env=False) as http:
        root, _, job = prepare(http)
        identity = job["output_version_id"]
        version = http.get(root + "/versions/" + identity).json()
        assert version["snapshot"]["review"]["graph_ready"]
        assert version["snapshot"]["entities"] and version["snapshot"]["source_refs"]
        assert version["snapshot"]["review"]["decisions"] == []
        checked = run_check(http, root, job, identity)
        assert checked["status"] == "completed"
        post(http, root + "/chat/sessions", {"version_id": identity}, 201)
        post(http, root + "/generation", {"version_id": identity}, 202)
        post(http, root + "/reviews/" + job["id"] + "/confirm", {}, 404)


def test_concurrent_checkpoints_create_one_graph_build():
    with httpx.Client(base_url="http://frontend", timeout=30, trust_env=False) as http:
        root, _, job = prepare(http)
        details = http.get(root + "/versions/" + job["output_version_id"] + "/review").json()
        issue = next(i for i in details["issues"] if i["requires_decision"])
        pending = post(
            http,
            root + "/reviews/" + job["id"] + "/decisions/draft",
            {
                "expected_version_id": job["output_version_id"],
                "conflict_id": issue["id"],
                "variant_id": issue["variants"][0]["id"],
                "signature": "Тест",
            },
            201,
        )
        path = root + "/reviews/" + job["id"] + "/graph-build"
        with ThreadPoolExecutor(max_workers=2) as executor:
            responses = list(
                executor.map(
                    lambda _: http.post(path, json={"version_id": pending["id"]}), range(2)
                )
            )
        assert all(response.status_code == 202 for response in responses)
        assert responses[0].json() == responses[1].json()
        assert wait(http, path)["status"] == "completed"
        assert len(http.get(root + "/versions").json()) == 4


def test_analysis_cannot_overwrite_a_version_saved_while_ml_was_running():
    with httpx.Client(base_url="http://frontend", timeout=30, trust_env=False) as http:
        root, _, job = prepare(http, "slow.docx", wait_analysis=False)
        deadline = time.monotonic() + 40
        while time.monotonic() < deadline:
            state = http.get(root + "/reviews/" + job["id"]).json()
            if state["output_version_id"]:
                break
            time.sleep(0.1)
        else:
            pytest.fail("Исходный граф не появился до завершения анализа")
        current = state["output_version_id"]
        manual = post(
            http,
            root + "/versions",
            {
                "expected_version_id": current,
                "snapshot": {"metadata": {"manual": "preserve"}},
                "signature": "Тестовый пользователь",
            },
            201,
        )
        final = wait(http, root + "/reviews/" + job["id"])
        assert final["status"] == "completed", final
        assert final["output_version_id"] != manual["id"]
        assert http.get(root).json()["current_version_id"] == manual["id"]


def test_review_history_can_be_replayed_and_completed_sse_has_a_terminal_state():
    import asyncio

    from temporalio.client import Client
    from temporalio.worker import Replayer

    from app.config import get_settings
    from app.review_workflow import ReviewWorkflow

    with httpx.Client(base_url="http://frontend", timeout=30, trust_env=False) as http:
        root, _, job = prepare(http)
        response = http.get(root + "/reviews/" + job["id"] + "/events")
        assert response.status_code == 200
        events = [
            json.loads(line.removeprefix("data: "))
            for line in response.text.splitlines()
            if line.startswith("data: ")
        ]
        assert len(events) == 1
        assert events[0]["status"] == "completed"

    async def replay():
        settings = get_settings()
        client = await Client.connect(
            settings.temporal_address, namespace=settings.temporal_namespace
        )
        handle = client.get_workflow_handle("review-" + job["id"])
        await handle.result()
        history = await handle.fetch_history()
        await Replayer(workflows=[ReviewWorkflow]).replay_workflow(history)

    asyncio.run(replay())
