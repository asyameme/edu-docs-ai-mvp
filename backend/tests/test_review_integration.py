"""Настоящий HTTP/PostgreSQL/Temporal: подготовка, выбор, проверка и подтверждение."""

import io
import json
import os
import time
import zipfile
from concurrent.futures import ThreadPoolExecutor
from uuid import uuid4

import httpx
import pytest

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


def prepare(http, filename="valid.docx", *, wait_analysis=True):
    program = post(http, "/api/v1/programs", {"name": "Искусственная проверка Backend4"}, 201)
    root = "/api/v1/programs/" + program["id"]
    stream = io.BytesIO()
    with zipfile.ZipFile(stream, "w") as archive:
        archive.writestr(filename, b"valid " + uuid4().bytes)
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
    issue = details["issues"][0]
    body = {
        "expected_version_id": job["output_version_id"],
        "conflict_id": issue["id"],
        "variant_id": issue["variants"][variant_index]["id"],
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


def test_complete_review_preserves_original_sources_versions_and_confirmation():
    with httpx.Client(base_url="http://frontend", timeout=30, trust_env=False) as http:
        root, imported, job = prepare(http)
        assert job["status"] == "completed", job
        original = http.get(root + "/versions/" + job["output_version_id"]).json()
        version, issue = choose(http, root, job)
        assert version["snapshot"]["entities"][0]["attributes"]["hours_total"] == 144
        checked = run_check(http, root, job, version["id"])
        assert checked["status"] == "completed", checked
        body = {"expected_version_id": version["id"], "check_job_id": checked["id"]}
        path = root + "/reviews/" + job["id"] + "/confirm"
        key = uuid4().hex
        confirmed = post(http, path, body, 201, key)
        assert post(http, path, body, 201, key)["id"] == confirmed["id"]
        assert http.get(root + "/versions/" + original["id"]).json() == original
        details = http.get(root + "/versions/" + confirmed["id"] + "/review").json()
        assert details["confirmed"] is True
        assert details["issues"][0]["id"] == issue["id"]
        assert details["issues"][0]["effective_status"] == "resolved"
        before_refs = {r["id"] for r in original["snapshot"]["source_refs"]}
        after_refs = {r["id"] for r in confirmed["snapshot"]["source_refs"]}
        assert before_refs.isdisjoint(after_refs)
        source = confirmed["snapshot"]["source_refs"][0]
        artifact = http.get(
            root
            + "/documents/"
            + source["document_id"]
            + "/artifacts/"
            + source["artifact_id"]
            + "/download"
        )
        assert artifact.status_code == 200
        assert artifact.text == "# Искусственный Markdown\n"
        assert len(http.get(root + "/versions").json()) == 4
        assert http.get(root + "/imports/" + imported["id"]).json()["status"] == "completed"
        restored = post(
            http,
            root + "/versions/" + confirmed["id"] + "/restore",
            {"expected_version_id": confirmed["id"]},
            201,
        )
        assert restored["snapshot"]["review"] is None
        assert http.get(root + "/versions/" + confirmed["id"] + "/review").json()["confirmed"]


@pytest.mark.parametrize("filename", ["warning.docx", "blocking.docx", "skipped.docx"])
def test_confirmation_gates_warning_blocking_and_skipped_rules(filename):
    with httpx.Client(base_url="http://frontend", timeout=30, trust_env=False) as http:
        root, _, job = prepare(http, filename)
        version, _ = choose(http, root, job)
        checked = run_check(http, root, job, version["id"])
        body = {"expected_version_id": version["id"], "check_job_id": checked["id"]}
        path = root + "/reviews/" + job["id"] + "/confirm"
        post(http, path, body, 409)
        if filename.startswith("warning"):
            body["accepted_warning_ids"] = [checked["report"]["issues"][0]["id"]]
            confirmed = post(http, path, body, 201)
            assert confirmed["snapshot"]["review"]["confirmation"]["accepted_warning_ids"]


def test_stale_check_and_concurrent_choices_do_not_overwrite_history():
    with httpx.Client(base_url="http://frontend", timeout=30, trust_env=False) as http:
        root, _, job = prepare(http)
        details = http.get(root + "/versions/" + job["output_version_id"] + "/review").json()
        issue = details["issues"][0]
        path = root + "/reviews/" + job["id"]
        requests = [
            {
                "expected_version_id": job["output_version_id"],
                "conflict_id": issue["id"],
                "variant_id": variant["id"],
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
            },
            201,
        )
        post(
            http,
            path + "/confirm",
            {"expected_version_id": changed["id"], "check_job_id": checked["id"]},
            409,
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
            assert len(http.get(root + "/versions").json()) == 1
            return
        assert job["status"] == "completed", job
        details = http.get(
            root + "/versions/" + job["output_version_id"] + "/review", params={"limit": 2}
        ).json()
        expected = (
            0 if filename.startswith("noconflict") else (19 if filename.startswith("many") else 1)
        )
        assert details["conflicts_count"] == expected
        assert len(details["issues"]) == min(2, expected)
        if expected == 0:
            checked = run_check(http, root, job, job["output_version_id"])
            post(
                http,
                root + "/reviews/" + job["id"] + "/confirm",
                {"expected_version_id": job["output_version_id"], "check_job_id": checked["id"]},
                201,
            )


def test_unresolved_conflict_foreign_ids_and_shared_operation_keys_are_rejected():
    with httpx.Client(base_url="http://frontend", timeout=30, trust_env=False) as http:
        root, imported, job = prepare(http)
        checked = run_check(http, root, job, job["output_version_id"])
        path = root + "/reviews/" + job["id"]
        post(
            http,
            path + "/confirm",
            {"expected_version_id": job["output_version_id"], "check_job_id": checked["id"]},
            409,
        )
        details = http.get(root + "/versions/" + job["output_version_id"] + "/review").json()
        issue = details["issues"][0]
        post(
            http,
            path + "/decisions",
            {
                "expected_version_id": job["output_version_id"],
                "conflict_id": issue["id"],
                "variant_id": str(uuid4()),
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


def test_concurrent_confirmation_creates_exactly_one_new_version():
    with httpx.Client(base_url="http://frontend", timeout=30, trust_env=False) as http:
        root, _, job = prepare(http)
        version, _ = choose(http, root, job)
        checked = run_check(http, root, job, version["id"])
        body = {"expected_version_id": version["id"], "check_job_id": checked["id"]}
        path = root + "/reviews/" + job["id"] + "/confirm"
        with ThreadPoolExecutor(max_workers=2) as executor:
            responses = list(
                executor.map(
                    lambda _: http.post(path, json=body, headers={"Idempotency-Key": uuid4().hex}),
                    range(2),
                )
            )
        assert sorted(response.status_code for response in responses) == [201, 409]
        assert len(http.get(root + "/versions").json()) == 4


def test_analysis_cannot_overwrite_a_version_saved_while_ml_was_running():
    with httpx.Client(base_url="http://frontend", timeout=30, trust_env=False) as http:
        root, _, job = prepare(http, "slow.docx", wait_analysis=False)
        current = http.get(root).json()["current_version_id"]
        manual = post(
            http,
            root + "/versions",
            {"expected_version_id": current, "snapshot": {"metadata": {"manual": "preserve"}}},
            201,
        )
        final = wait(http, root + "/reviews/" + job["id"])
        assert final["status"] == "conflict", final
        assert final["output_version_id"] is None
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
