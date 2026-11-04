"""Истории старого и нового импорта воспроизводятся после изменения хранилища."""

import asyncio
import io
import os
import time
import zipfile
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient
from psycopg.types.json import Jsonb
from temporalio.client import Client
from temporalio.worker import Replayer

from app.config import get_settings
from app.main import app
from app.workflows import ImportWorkflow, LegacyImportWorkflow

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(os.getenv("RUN_INTEGRATION") != "1", reason="Needs isolated services"),
]


def wait(client, program_id, job_id):
    deadline = time.monotonic() + 30
    path = f"/api/v1/programs/{program_id}/imports/{job_id}"
    while time.monotonic() < deadline:
        response = client.get(path)
        assert response.status_code == 200, response.text
        state = response.json()
        if state["status"] in {"completed", "partial", "failed"}:
            return state
        time.sleep(0.2)
    pytest.fail("Import did not finish")


async def replay(job_id, cls):
    settings = get_settings()
    client = await Client.connect(settings.temporal_address, namespace=settings.temporal_namespace)
    handle = client.get_workflow_handle(f"import-{job_id}-1")
    await handle.result()
    history = await handle.fetch_history()
    await Replayer(workflows=[cls]).replay_workflow(history)


def test_legacy_import_v1_history_remains_supported():
    with TestClient(app) as client:
        program = client.post("/api/v1/programs", json={"name": "Старый процесс V1"}).json()
        job_id, key = uuid4(), uuid4().hex
        with app.state.resources.postgres.connection() as connection:
            connection.execute(
                """INSERT INTO processing_jobs(
                   id,program_id,input_version_id,operation_key,request_hash,type,
                   temporal_namespace,workflow_id,parameters,report)
                   VALUES(%s,%s,%s,%s,%s,'import',%s,%s,%s,%s)""",
                (
                    job_id,
                    UUID(program["id"]),
                    UUID(program["current_version_id"]),
                    key,
                    uuid4().hex * 2,
                    get_settings().temporal_namespace,
                    f"import-{job_id}-1",
                    Jsonb({"documents": [], "request_keys": [key], "fingerprint": uuid4().hex * 2}),
                    Jsonb({"legacy_import": True}),
                ),
            )
        assert wait(client, program["id"], str(job_id))["status"] == "failed"
        asyncio.run(replay(job_id, LegacyImportWorkflow))


def test_import_v2_history_is_replayable():
    with TestClient(app) as client:
        program = client.post("/api/v1/programs", json={"name": "Новый процесс V2"}).json()
        stream = io.BytesIO()
        with zipfile.ZipFile(stream, "w") as archive:
            archive.writestr("source.docx", b"replay-check-" + uuid4().bytes)
        response = client.post(
            f"/api/v1/programs/{program['id']}/imports",
            files={"file": ("package.zip", stream.getvalue())},
            headers={"Idempotency-Key": uuid4().hex},
        )
        assert response.status_code == 202, response.text
        job = response.json()
        state = wait(client, program["id"], job["id"])
        assert len(state["files"]) == 1
        asyncio.run(replay(job["id"], ImportWorkflow))
