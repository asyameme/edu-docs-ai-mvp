"""Исходные байты, ограничения и атомарность хранения в настоящем PostgreSQL."""

import hashlib
import os
from io import BytesIO
from uuid import uuid4

import psycopg
import pytest
from fastapi.testclient import TestClient
from starlette.datastructures import UploadFile

from app.import_repository import ImportRepository
from app.main import app
from app.repository import Repository
from app.storage import OriginalStorage

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(os.getenv("RUN_INTEGRATION") != "1", reason="Needs isolated PostgreSQL"),
]


def test_original_bytes_are_stored_in_postgres_and_returned_unchanged():
    raw = bytes(range(256)) * 100
    with TestClient(app) as client:
        program = client.post("/api/v1/programs", json={"name": "Binary original"}).json()
        base = "/api/v1/programs/" + program["id"]
        response = client.post(base + "/documents", files={"file": ("оригинал.pdf", raw)})
        assert response.status_code == 201, response.text
        document = response.json()
        with app.state.resources.postgres.connection() as connection:
            stored = connection.execute(
                "SELECT content FROM source_documents WHERE id=%s", (document["id"],)
            ).fetchone()[0]
        assert bytes(stored) == raw
        assert client.get(base + "/documents/" + document["id"] + "/download").content == raw
        assert "content" not in document
        assert "content" not in client.get(base + "/documents").json()[0]


@pytest.mark.parametrize("wrong_size,wrong_hash", [(True, False), (False, True)])
def test_database_rejects_bytes_that_disagree_with_metadata(wrong_size, wrong_hash):
    raw = b"immutable original"
    with TestClient(app) as client:
        program = client.post("/api/v1/programs", json={"name": "Binary constraint"}).json()
        with pytest.raises(psycopg.errors.CheckViolation):
            with app.state.resources.postgres.connection() as connection:
                connection.execute(
                    """INSERT INTO source_documents
                    (id,program_id,original_name,format,media_type,size_bytes,sha256,content)
                    VALUES(%s,%s,'source.pdf','pdf','application/pdf',%s,%s,%s)""",
                    (
                        uuid4(),
                        program["id"],
                        len(raw) + int(wrong_size),
                        "0" * 64 if wrong_hash else hashlib.sha256(raw).hexdigest(),
                        raw,
                    ),
                )


def test_database_rejects_modification_of_original_bytes():
    with TestClient(app) as client:
        program = client.post("/api/v1/programs", json={"name": "Immutable binary"}).json()
        base = "/api/v1/programs/" + program["id"]
        document = client.post(
            base + "/documents", files={"file": ("source.pdf", b"original")}
        ).json()
        with pytest.raises(psycopg.errors.CheckViolation):
            with app.state.resources.postgres.connection() as connection:
                connection.execute(
                    "UPDATE source_documents SET content=%s WHERE id=%s",
                    (b"changed!", document["id"]),
                )
        assert (
            client.get(base + "/documents/" + document["id"] + "/download").content == b"original"
        )


@pytest.mark.parametrize("package", [False, True])
def test_failure_after_original_insert_rolls_back_bytes_metadata_and_job(monkeypatch, package):
    with TestClient(app) as client:
        program = client.post("/api/v1/programs", json={"name": "Atomic original"}).json()
        pool = app.state.resources.postgres
        stored = OriginalStorage(1024).save(UploadFile(BytesIO(b"raw"), filename="source.pdf"))

        def fail(*args):
            raise RuntimeError("Failure after original INSERT")

        if package:
            monkeypatch.setattr("app.import_repository.Jsonb", fail)

            def operation():
                return ImportRepository(pool).create(
                    program["id"], "fingerprint", str(uuid4()), [("source.pdf", stored)]
                )
        else:
            insert = Repository.insert_document

            def insert_then_fail(*args):
                insert(*args)
                fail()

            monkeypatch.setattr(Repository, "insert_document", staticmethod(insert_then_fail))

            def operation():
                return Repository(pool).add_document(program["id"], stored)

        with pytest.raises(RuntimeError, match="after original INSERT"):
            operation()
        with pool.connection() as connection:
            assert (
                connection.execute(
                    "SELECT count(*) FROM source_documents WHERE program_id=%s", (program["id"],)
                ).fetchone()[0]
                == 0
            )
            assert (
                connection.execute(
                    "SELECT count(*) FROM processing_jobs WHERE program_id=%s", (program["id"],)
                ).fetchone()[0]
                == 0
            )
