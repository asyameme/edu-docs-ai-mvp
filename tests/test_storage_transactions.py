"""Искусственные сбои проверяют настоящий откат транзакций в БД."""

import os
from io import BytesIO
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient
from starlette.datastructures import UploadFile

from app.domain import Snapshot
from app.main import app
from app.repository import Repository
from app.storage import OriginalStorage

live = pytest.mark.skipif(os.getenv("RUN_INTEGRATION") != "1", reason="Needs isolated PostgreSQL")


@live
@pytest.mark.integration
def test_failure_after_version_insert_rolls_back_version_and_head(monkeypatch):
    with TestClient(app) as client:
        program = client.post("/api/v1/programs", json={"name": "Rollback acceptance"}).json()
        repo = Repository(app.state.resources.postgres)

        def fail(*args):
            raise RuntimeError("Injected failure after version INSERT")

        monkeypatch.setattr(repo, "attach_documents", fail)
        with pytest.raises(RuntimeError, match="after version INSERT"):
            repo.save(
                UUID(program["id"]),
                UUID(program["current_version_id"]),
                str(uuid4()),
                snapshot=Snapshot(),
            )
        assert len(repo.versions(program["id"])) == 1
        assert (
            str(repo.program(program["id"])["current_version_id"]) == program["current_version_id"]
        )
        # Повтор после отката создаёт V2 без пропуска номера и сохранения неудачного ключа.
        monkeypatch.undo()
        result = client.post(
            "/api/v1/programs/" + program["id"] + "/versions",
            json={
                "expected_version_id": program["current_version_id"],
                "snapshot": Snapshot().model_dump(mode="json"),
            },
            headers={"Idempotency-Key": str(uuid4())},
        )
        assert result.status_code == 201
        assert result.json()["number"] == 2


@live
@pytest.mark.integration
def test_db_failure_removes_new_original_without_document_row(monkeypatch, tmp_path):
    from app.program_api import storage

    with TestClient(app, raise_server_exceptions=False) as client:
        program = client.post("/api/v1/programs", json={"name": "Upload rollback"}).json()
        app.dependency_overrides[storage] = lambda: OriginalStorage(tmp_path, 1024)

        def fail(*args):
            raise RuntimeError("Injected DB failure")

        monkeypatch.setattr(Repository, "add_document", fail)
        try:
            response = client.post(
                "/api/v1/programs/" + program["id"] + "/documents",
                files={"file": ("test.pdf", b"Original bytes")},
            )
            assert response.status_code == 500
            assert list(tmp_path.iterdir()) == []
            assert client.get("/api/v1/programs/" + program["id"] + "/documents").json() == []
        finally:
            app.dependency_overrides.clear()


def test_failed_file_promotion_leaves_no_partial_file(monkeypatch, tmp_path):
    import app.storage as storage_module

    def fail(*args):
        raise OSError("Injected disk failure")

    monkeypatch.setattr(storage_module.os, "replace", fail)
    with pytest.raises(OSError):
        OriginalStorage(tmp_path, 100).save(UploadFile(BytesIO(b"original"), filename="a.pdf"))
    assert list(tmp_path.iterdir()) == []


@live
@pytest.mark.integration
def test_database_refuses_historical_snapshot_mutation():
    import psycopg

    with TestClient(app) as client:
        program = client.post("/api/v1/programs", json={"name": "Immutable history"}).json()
        pool = app.state.resources.postgres
        with pytest.raises(psycopg.errors.CheckViolation):
            with pool.connection() as connection:
                connection.execute(
                    "UPDATE program_versions SET snapshot = %s WHERE id=%s",
                    (
                        '{"schema_version":1,"metadata":{"overwritten":true}}',
                        program["current_version_id"],
                    ),
                )
        assert (
            client.get(
                "/api/v1/programs/" + program["id"] + "/versions/" + program["current_version_id"]
            ).json()["snapshot"]["metadata"]
            == {}
        )
