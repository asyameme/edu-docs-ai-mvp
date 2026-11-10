"""Реальные PostgreSQL и HTTP: задача генерации, ZIP и повторный запрос."""

import hashlib
import io
import json
import os
import time
from pathlib import Path
from uuid import UUID, uuid4
from zipfile import ZipFile

import pytest
from fastapi.testclient import TestClient
from psycopg.types.json import Jsonb
from starlette.datastructures import UploadFile
from test_generation_docx_patch import document_text, make_docx

from app.domain import SnapshotV2
from app.generation_activities import GenerationActivities
from app.generation_repository import GenerationRepository
from app.import_repository import ImportRepository
from app.main import app
from app.repository import Repository
from app.storage import OriginalStorage
from worker.generation import read_matrix_xlsx

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(os.getenv("RUN_INTEGRATION") != "1", reason="Нужен PostgreSQL"),
]


def test_confirmed_version_generates_durable_zip_and_rejects_duplicate_key():
    content = make_docx("<w:p><w:r><w:t>Алгебра 72</w:t></w:r></w:p>")
    with TestClient(app) as client:
        program = client.post("/api/v1/programs", json={"name": "Генерация"}).json()
        program_id = UUID(program["id"])
        root = f"/api/v1/programs/{program_id}/generation"
        assert client.post(
            root,
            json={"version_id": program["current_version_id"]},
            headers={"Idempotency-Key": uuid4().hex},
        ).status_code == 409

        stored = OriginalStorage(1024 * 1024).save(
            UploadFile(file=io.BytesIO(content), filename="Алгебра.docx")
        )
        imported_id, _ = ImportRepository(app.state.resources.postgres).create(
            program_id, uuid4().hex, uuid4().hex, [("РПД/Алгебра.docx", stored, True)]
        )
        with app.state.resources.postgres.connection() as connection:
            document_id = connection.execute(
                "SELECT id FROM source_documents WHERE program_id=%s AND sha256=%s",
                (program_id, stored["sha256"]),
            ).fetchone()[0]
            connection.execute(
                "UPDATE processing_jobs SET report=%s WHERE id=%s",
                (
                    Jsonb(
                        {
                            "outcome": "completed",
                            "files": {
                                str(document_id): {
                                    "output": {
                                        "meta": {
                                            "doc_type": "RPD",
                                            "file_hash": stored["sha256"],
                                        }
                                    }
                                }
                            },
                        }
                    ),
                    imported_id,
                ),
            )
        discipline, competency, relation, ref = [uuid4() for _ in range(4)]
        snapshot = SnapshotV2.model_validate(
            {
                "document_ids": [document_id],
                "entities": [
                    {
                        "id": discipline,
                        "kind": "discipline",
                        "attributes": {"name": "Алгебра", "hours_total": 108},
                        "source_ref_ids": [ref],
                    },
                    {
                        "id": competency,
                        "kind": "competency",
                        "attributes": {"code": "УК-1"},
                        "source_ref_ids": [ref],
                    },
                ],
                "relations": [
                    {
                        "id": relation,
                        "source_id": discipline,
                        "target_id": competency,
                        "type": "FORMS_COMPETENCY",
                        "source_ref_ids": [ref],
                    }
                ],
                "source_refs": [
                    {
                        "id": ref,
                        "document_id": document_id,
                        "locator": json.dumps({"file": "Алгебра.docx", "paragraph_index": 1}),
                    }
                ],
                "source_links": [
                    {
                        "id": uuid4(),
                        "target_kind": "entity",
                        "target_id": discipline,
                        "field_path": "/attributes/hours_total",
                        "source_ref_id": ref,
                        "observed_value": {"value": 72},
                    }
                ],
                "review": {
                    "review_id": uuid4(),
                    "import_job_id": imported_id,
                    "analysis_version_id": uuid4(),
                    "decisions": [
                        {
                            "conflict_id": uuid4(),
                            "variant_id": uuid4(),
                            "check_job_id": uuid4(),
                            "changes": [
                                {
                                    "op": "set_attribute",
                                    "target_kind": "entity",
                                    "target_id": str(discipline),
                                    "field_path": "/attributes/hours_total",
                                    "value": 108,
                                }
                            ],
                            "decided_at": "2026-11-10T00:00:00Z",
                        }
                    ],
                    "confirmation": {
                        "checked_version_id": uuid4(),
                        "check_job_id": uuid4(),
                        "content_hash": "0" * 64,
                        "accepted_warning_ids": [],
                        "confirmed_at": "2026-11-10T00:00:00Z",
                    },
                },
            }
        )
        version = Repository(app.state.resources.postgres).save(
            program_id, UUID(program["current_version_id"]), uuid4().hex, snapshot
        )
        body = {"version_id": str(version["id"])}
        key = uuid4().hex
        created = client.post(root, json=body, headers={"Idempotency-Key": key})
        assert created.status_code == 202, created.text
        job_id = created.json()["id"]
        assert client.post(root, json=body, headers={"Idempotency-Key": key}).json()["id"] == job_id
        if os.getenv("GENERATION_WORKER_TESTS") != "1":
            assert client.get(f"{root}/{job_id}/download").status_code == 409
        activity = GenerationActivities(app.state.resources.postgres)
        if os.getenv("GENERATION_WORKER_TESTS") == "1":
            deadline = time.monotonic() + 60
            while time.monotonic() < deadline:
                if client.get(f"{root}/{job_id}").json()["status"] == "completed":
                    break
                time.sleep(0.2)
            else:
                pytest.fail("Воркер не завершил генерацию")
        else:
            assert activity.build({"job_id": job_id}) == "completed"
        assert activity.build({"job_id": job_id}) == "completed"
        ready = client.get(f"{root}/{job_id}").json()
        assert ready["status"] == "completed"
        downloaded = client.get(f"{root}/{job_id}/download")
        assert downloaded.status_code == 200
        assert downloaded.headers["content-type"] == "application/zip"
        assert hashlib.sha256(downloaded.content).hexdigest() == ready["sha256"]
        with ZipFile(io.BytesIO(downloaded.content)) as archive:
            assert set(archive.namelist()) == {
                "РПД_Алгебра.docx",
                "Матрица компетенций.xlsx",
            }
            generated_docx = archive.read("РПД_Алгебра.docx")
            assert generated_docx != content
            assert "Алгебра 108" in document_text(generated_docx)
            codes, rows = read_matrix_xlsx(archive.read("Матрица компетенций.xlsx"))
            assert codes == ("УК-1",)
            assert rows == [[1]]
        assert client.post(
            root,
            json={"version_id": program["current_version_id"]},
            headers={"Idempotency-Key": key},
        ).status_code == 409


@pytest.mark.skipif(
    os.getenv("GENERATION_REAL_DOC_TESTS") != "1", reason="Нужен тестовый Worker и оригиналы"
)
def test_real_archive_import_to_generated_zip_without_analysis():
    originals = Path(__file__).resolve().parents[2] / "tests" / "originals"
    members = {
        "РПД/РПД_матанализ1.docx": (originals / "РПД_матанализ1.docx").read_bytes(),
        "РПД/РПД_БД.docx": (originals / "РПД_БД.docx").read_bytes(),
        "РПП/РПП.docx": (originals / "РПП.docx").read_bytes(),
        "Учебный план/UP.docx": (originals / "UP.docx").read_bytes(),
        "Характеристика/Общая характеристика.docx": (
            originals / "Общая характеристика.docx"
        ).read_bytes(),
        "ЦКМВ/ЦКМВ.docx": (originals / "ЦКМВ.docx").read_bytes(),
        "Матрица компетенций/Матрица компетенций.xlsx": (
            originals / "Матрица компетенций.xlsx"
        ).read_bytes(),
    }
    archive_bytes = io.BytesIO()
    with ZipFile(archive_bytes, "w") as archive:
        for name, content in members.items():
            archive.writestr(name, content)
    with TestClient(app) as client:
        program = client.post("/api/v1/programs", json={"name": "Реальный архив"}).json()
        root = f"/api/v1/programs/{program['id']}"
        uploaded = client.post(
            root + "/imports",
            files={"file": ("данные.zip", archive_bytes.getvalue())},
            headers={"Idempotency-Key": uuid4().hex},
        )
        assert uploaded.status_code == 202, uploaded.text
        import_id = uploaded.json()["id"]
        deadline = time.monotonic() + 90
        while time.monotonic() < deadline:
            imported = client.get(root + f"/imports/{import_id}").json()
            if imported["status"] in {"completed", "partial", "failed"}:
                break
            time.sleep(0.25)
        else:
            pytest.fail("Парсер не завершил обработку архива")
        assert len(imported["files"]) == 6
        assert all(item["status"] == "completed" for item in imported["files"]), imported
        accepted = client.post(
            root + "/generation",
            json={"import_job_id": import_id},
            headers={"Idempotency-Key": uuid4().hex},
        )
        assert accepted.status_code == 202, accepted.text
        assert accepted.json()["version_id"] is None
        assert accepted.json()["import_job_id"] == import_id
        job_id = accepted.json()["id"]
        deadline = time.monotonic() + 90
        while time.monotonic() < deadline:
            generated = client.get(root + f"/generation/{job_id}").json()
            if generated["status"] in {"completed", "failed"}:
                break
            time.sleep(0.25)
        else:
            pytest.fail("Воркер не завершил генерацию реального архива")
        assert generated["status"] == "completed", generated
        downloaded = client.get(root + f"/generation/{job_id}/download")
        assert downloaded.status_code == 200
        with ZipFile(io.BytesIO(downloaded.content)) as archive:
            for name, content in members.items():
                if name.endswith(".docx"):
                    assert archive.read(name.replace("/", "_")) == content
            assert len(archive.namelist()) == 7
            codes, rows = read_matrix_xlsx(archive.read("Матрица компетенций.xlsx"))
            assert codes and rows


@pytest.mark.skipif(
    os.getenv("GENERATION_PIN_TESTS") != "1", reason="Нужна тестовая БД без запущенного Worker"
)
def test_generation_keeps_import_result_after_retry():
    content = make_docx("<w:p><w:r><w:t>Алгебра</w:t></w:r></w:p>")
    with TestClient(app):
        repo = Repository(app.state.resources.postgres)
        program = repo.create_program("Повтор импорта при генерации")
        program_id = program["id"]
        stored = OriginalStorage(1024 * 1024).save(
            UploadFile(file=io.BytesIO(content), filename="Алгебра.docx")
        )
        imports = ImportRepository(app.state.resources.postgres)
        import_id, _ = imports.create(
            program_id, uuid4().hex, uuid4().hex, [("РПД/Алгебра.docx", stored, True)]
        )
        with app.state.resources.postgres.connection() as connection:
            document_id = connection.execute(
                "SELECT id FROM source_documents WHERE program_id=%s AND sha256=%s",
                (program_id, stored["sha256"]),
            ).fetchone()[0]
            workflow_id = connection.execute(
                "SELECT workflow_id FROM processing_jobs WHERE id=%s", (import_id,)
            ).fetchone()[0]
            snapshot = SnapshotV2.model_validate({
                "document_ids": [document_id],
                "entities": [
                    {"id": uuid4(), "kind": "discipline", "attributes": {"name": "Алгебра"}},
                    {"id": uuid4(), "kind": "competency", "attributes": {"code": "УК-1"}},
                ],
            })
            connection.execute(
                "UPDATE processing_jobs SET report=%s WHERE id=%s",
                (
                    Jsonb({
                        "outcome": "partial",
                        "snapshot": snapshot.model_dump(mode="json"),
                        "files": {str(document_id): {"output": {
                            "meta": {"doc_type": "RPD", "file_hash": stored["sha256"]}
                        }}},
                    }),
                    import_id,
                ),
            )
        generations = GenerationRepository(app.state.resources.postgres)
        job_id = generations.create(program_id, None, import_id, uuid4().hex)
        imports.retry(program_id, import_id, workflow_id)
        with app.state.resources.postgres.connection() as connection:
            assert connection.execute(
                "SELECT report ? 'snapshot' FROM processing_jobs WHERE id=%s", (import_id,)
            ).fetchone()[0] is False
        assert GenerationActivities(app.state.resources.postgres).build(
            {"job_id": str(job_id)}
        ) == "completed"
        with ZipFile(io.BytesIO(generations.package(program_id, job_id)["content"])) as archive:
            assert archive.read("РПД_Алгебра.docx") == content
