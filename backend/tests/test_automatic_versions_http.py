"""Настоящие DOCX → HTTP → Temporal → исходный граф → проверка → ZIP без confirm."""

import asyncio
import io
import os
import time
from pathlib import Path
from uuid import uuid4
from zipfile import ZipFile

import httpx
import pytest
from docx import Document

from app.local_parser import parse_local_document

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        os.getenv("AUTOMATIC_VERSION_TESTS") != "1",
        reason="Нужен отдельный стек с автоматическими версиями",
    ),
]
ORIGINALS = Path(__file__).resolve().parents[2] / "tests" / "originals"


def wait_job(http, path):
    deadline = time.monotonic() + 90
    while time.monotonic() < deadline:
        response = http.get(path)
        assert response.status_code == 200, response.text
        job = response.json()
        if job["status"] not in {"pending", "processing"}:
            return job
        time.sleep(0.2)
    pytest.fail("Изолированная задача не завершилась за 90 секунд")


def test_real_source_graph_and_package_exist_before_any_warning_acceptance_or_choice():
    original = (ORIGINALS / "РПД_матанализ1.docx").read_bytes()
    parsed = parse_local_document("РПД/source.docx", original, uuid4())
    formulation = parsed["competencies"][0]["formulation"]
    alternative = "способен создавать прогнозы на основе статистических моделей"
    word = Document(io.BytesIO(original))
    matches = [
        cell
        for table in word.tables
        for row in table.rows
        for cell in row.cells
        if formulation in cell.text
    ]
    assert len(matches) == 1
    matches[0].text = matches[0].text.replace(formulation, alternative)
    modified = io.BytesIO()
    word.save(modified)
    sources = {
        "изменённая.docx": modified.getvalue(),
        "Общая характеристика.docx": (ORIGINALS / "Общая характеристика.docx").read_bytes(),
    }
    package = io.BytesIO()
    with ZipFile(package, "w") as archive:
        for name, content in sources.items():
            archive.writestr(name, content)
    with httpx.Client(base_url="http://frontend", timeout=30, trust_env=False) as http:
        response = http.post("/api/v1/programs", json={"name": "Приёмка исходного графа"})
        assert response.status_code == 201
        program = response.json()
        root = "/api/v1/programs/" + program["id"]
        response = http.post(
            root + "/imports",
            files={"file": ("sources.zip", package.getvalue())},
            headers={"Idempotency-Key": uuid4().hex},
        )
        assert response.status_code == 202, response.text
        imported = wait_job(http, root + "/imports/" + response.json()["id"])
        assert imported["status"] in {"partial", "completed"}, imported
        assert imported["automatic_review_id"]

        review_id = imported["automatic_review_id"]
        review = wait_job(http, root + "/reviews/" + review_id)
        assert review["status"] == "completed", review
        source_id = review["output_version_id"]
        details_url = root + "/versions/" + source_id + "/review?limit=100"
        details = http.get(details_url).json()
        assert details["lifecycle"]["saved"] and details["lifecycle"]["is_source"]
        assert details["version"]["snapshot"]["review"]["decisions"] == []
        assert details["version"]["snapshot"]["review"]["confirmation"] is None
        assert any(issue["requires_decision"] for issue in details["issues"])
        check_id = details["lifecycle"]["check_job_id"]
        assert check_id
        checked = wait_job(http, root + "/reviews/" + review_id + "/checks/" + check_id)
        assert checked["status"] == "completed", checked
        details = http.get(details_url).json()
        assert len(details["check_report"]["coverage"]) == 13
        assert details["lifecycle"]["can_generate"], details["lifecycle"]

        response = http.post(
            root + "/generation",
            json={"version_id": source_id},
            headers={"Idempotency-Key": uuid4().hex},
        )
        assert response.status_code == 202, response.text
        generated = wait_job(http, root + "/generation/" + response.json()["id"])
        assert generated["status"] == "completed", generated
        downloaded = http.get(root + "/generation/" + generated["id"] + "/download")
        assert downloaded.status_code == 200
        with ZipFile(io.BytesIO(downloaded.content)) as archive:
            for name, content in sources.items():
                assert archive.read(name) == content
            assert any(name.endswith(".xlsx") for name in archive.namelist())
        assert http.get(details_url).json()["version"]["snapshot"]["review"]["confirmation"] is None

        conflict = next(
            issue
            for issue in details["issues"]
            if issue["requires_decision"] and issue.get("variants")
            and any(change.get("expected_old_value") == alternative
                    for variant in issue["variants"] for change in variant["changes"])
        )
        selected = conflict["variants"][0]
        selected = next(variant for variant in conflict["variants"] if any(
            change.get("expected_old_value") == alternative and change.get("value") != alternative
            for change in variant["changes"]
        ))
        selected_formulation = next(change["value"] for change in selected["changes"]
                                    if change.get("expected_old_value") == alternative)
        response = http.post(
            root + "/reviews/" + review_id + "/decisions",
            json={
                "expected_version_id": source_id,
                "conflict_id": conflict["id"],
                "variant_id": selected["id"],
                "signature": "Изолированная приёмка",
            },
            headers={"Idempotency-Key": uuid4().hex},
        )
        assert response.status_code == 201, response.text
        changed = response.json()
        assert changed["id"] != source_id and changed["snapshot"]["review"]["graph_ready"]
        assert http.get(details_url).json()["version"]["snapshot"]["review"]["decisions"] == []
        changed_url = root + "/versions/" + changed["id"] + "/review?limit=100"
        changed_details = http.get(changed_url).json()
        check = wait_job(http, root + "/reviews/" + review_id + "/checks/"
                         + changed_details["lifecycle"]["check_job_id"])
        assert check["status"] == "completed", check
        response = http.post(root + "/generation", json={"version_id": changed["id"]},
                             headers={"Idempotency-Key": uuid4().hex})
        assert response.status_code == 202, response.text
        generated = wait_job(http, root + "/generation/" + response.json()["id"])
        assert generated["status"] == "completed", generated
        downloaded = http.get(root + "/generation/" + generated["id"] + "/download")
        with ZipFile(io.BytesIO(downloaded.content)) as archive:
            rendered = Document(io.BytesIO(archive.read("изменённая.docx")))
            text = "\n".join(c.text for t in rendered.tables for row in t.rows for c in row.cells)
            assert selected_formulation in text and alternative not in text

        response = http.post(root + "/chat/sessions", json={"version_id": source_id},
                             headers={"Idempotency-Key": uuid4().hex})
        assert response.status_code == 201, response.text
        session = response.json()
        response = http.post(root + "/chat/sessions/" + session["id"] + "/messages",
                             json={"text": "fixture-summary"},
                             headers={"Idempotency-Key": uuid4().hex})
        assert response.status_code == 202, response.text
        events = http.get(root + "/chat/runs/" + response.json()["run_id"] + "/events", timeout=90)
        assert "event: completed" in events.text, events.text
        assert "Сводка получена" in events.text

        response = http.post(root + "/chat/sessions", json={"version_id": changed["id"]},
                             headers={"Idempotency-Key": uuid4().hex})
        assert response.status_code == 201, response.text
        session = response.json()
        chat_path = root + "/chat/sessions/" + session["id"]
        response = http.post(chat_path + "/messages", json={
            "text": "fixture-query:" + selected_formulation + "|fixture-formulation измени",
        }, headers={"Idempotency-Key": uuid4().hex})
        assert response.status_code == 202, response.text
        turn = response.json()["run_id"]
        events = http.get(root + "/chat/runs/" + turn + "/events", timeout=90)
        assert "event: completed" in events.text, events.text
        history = http.get(chat_path).json()
        assert history["turns"][-1]["result"]["kind"] == "change_proposal"
        response = http.post(chat_path + "/runs/" + turn + "/accept", json={
            "expected_version_id": changed["id"], "signature": "Проверка чата",
        }, headers={"Idempotency-Key": uuid4().hex})
        assert response.status_code == 201, response.text
        accepted = response.json()
        chatted = accepted["version"]
        assert chatted["snapshot"]["review"]["confirmation"] is None
        assert len(chatted["snapshot"]["review"]["decisions"]) == 2
        check = wait_job(http, root + "/reviews/" + review_id + "/checks/"
                         + accepted["check_job_id"])
        assert check["status"] == "completed", check
        response = http.post(root + "/generation", json={"version_id": chatted["id"]},
                             headers={"Idempotency-Key": uuid4().hex})
        assert response.status_code == 202, response.text
        generated = wait_job(http, root + "/generation/" + response.json()["id"])
        assert generated["status"] == "completed", generated
        downloaded = http.get(root + "/generation/" + generated["id"] + "/download")
        with ZipFile(io.BytesIO(downloaded.content)) as archive:
            texts = []
            for name in sources:
                rendered = Document(io.BytesIO(archive.read(name)))
                texts.append("\n".join(c.text for t in rendered.tables
                                       for row in t.rows for c in row.cells))
            assert any("Уточнённая формулировка после чата" in text for text in texts)
        response = http.post(chat_path + "/runs/" + turn + "/undo", json={
            "expected_version_id": chatted["id"], "signature": "Проверка отмены",
        }, headers={"Idempotency-Key": uuid4().hex})
        assert response.status_code == 201, response.text
        undone = response.json()
        assert undone["snapshot"]["review"]["lifecycle_policy"] == "automatic_source_v2"
        assert len(undone["snapshot"]["review"]["decisions"]) == 1
        undone_details = http.get(root + "/versions/" + undone["id"] + "/review").json()
        assert undone_details["lifecycle"]["check_job_id"]

        async def replay_preparation():
            from temporalio.client import Client
            from temporalio.worker import Replayer

            from app.config import get_settings
            from app.review_workflow import ReviewWorkflow

            settings = get_settings()
            client = await Client.connect(
                settings.temporal_address, namespace=settings.temporal_namespace,
            )
            handle = client.get_workflow_handle("review-" + review_id)
            assert await handle.result() == "completed"
            await Replayer(workflows=[ReviewWorkflow]).replay_workflow(
                await handle.fetch_history(),
            )

        asyncio.run(replay_preparation())
