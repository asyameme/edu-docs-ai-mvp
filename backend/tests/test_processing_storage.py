"""Согласование рабочего хранилища с бизнес-таблицей задач из черновика."""

import io
import os
import zipfile
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient
from psycopg.rows import dict_row

from app.main import app

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(os.getenv("RUN_INTEGRATION") != "1", reason="Needs isolated PostgreSQL"),
]


def test_processing_jobs_has_business_fields_without_execution_state():
    with TestClient(app):
        with app.state.resources.postgres.connection() as connection:
            columns = {
                row[0]
                for row in connection.execute(
                    """SELECT column_name FROM information_schema.columns
                       WHERE table_schema=current_schema() AND table_name='processing_jobs'"""
                )
            }
            assert columns == {
                "id",
                "program_id",
                "input_version_id",
                "output_version_id",
                "operation_key",
                "request_hash",
                "type",
                "temporal_namespace",
                "workflow_id",
                "parameters",
                "report",
                "created_at",
            }
            legacy = connection.execute(
                """SELECT tablename FROM pg_tables WHERE schemaname=current_schema()
                   AND tablename IN ('import_jobs','import_files','import_requests')"""
            ).fetchall()
            assert legacy == []


def test_import_records_input_version_documents_and_all_request_keys():
    with TestClient(app) as client:
        program = client.post("/api/v1/programs", json={"name": "Бизнес-запись импорта"}).json()
        stream = io.BytesIO()
        with zipfile.ZipFile(stream, "w") as archive:
            archive.writestr("source.docx", b"artificial processing storage test " + uuid4().bytes)
        endpoint = f"/api/v1/programs/{program['id']}/imports"
        keys = [uuid4().hex, uuid4().hex]
        responses = [
            client.post(
                endpoint,
                files={"file": ("package.zip", stream.getvalue())},
                headers={"Idempotency-Key": key},
            )
            for key in keys
        ]
        assert all(response.status_code == 202 for response in responses)
        job_id = responses[0].json()["id"]
        assert responses[1].json()["id"] == job_id
        with (
            app.state.resources.postgres.connection() as connection,
            connection.cursor(row_factory=dict_row) as cursor,
        ):
            row = cursor.execute(
                "SELECT * FROM processing_jobs WHERE id=%s", (UUID(job_id),)
            ).fetchone()
        assert str(row["input_version_id"]) == program["current_version_id"]
        assert row["output_version_id"] is None
        assert row["type"] == "import"
        assert set(row["parameters"]["request_keys"]) == set(keys)
        assert len(row["parameters"]["documents"]) == 1
        assert row["workflow_id"].startswith("import-" + job_id)
        assert row["temporal_namespace"] == "education"
