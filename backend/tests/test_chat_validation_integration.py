"""Сквозной запуск chat check через API, PostgreSQL и Temporal."""

import os
import time
from uuid import UUID

import httpx
import pytest
from psycopg.types.json import Jsonb
from psycopg_pool import ConnectionPool
from test_review_integration import choose, prepare

from app.chat_validation import ChatValidationTools
from app.config import get_settings


@pytest.mark.integration
@pytest.mark.skipif(
    os.getenv("REVIEW_FIXTURE_TESTS") != "1",
    reason="Требуются отдельные тестовые HTTP, PostgreSQL и Temporal сервисы",
)
def test_chat_check_tool_uses_existing_review_job_and_keeps_version():
    settings = get_settings()
    with httpx.Client(base_url="http://frontend", timeout=30, trust_env=False) as http:
        root, _, review = prepare(http, "РПД/РПД_матанализ1.docx")
        version, _ = choose(http, root, review)
        program_id = UUID(version["program_id"])
        version_id = UUID(version["id"])
        pool = ConnectionPool(
            kwargs={
                "host": settings.postgres_host,
                "dbname": settings.postgres_db,
                "user": settings.postgres_user,
                "password": settings.postgres_password.get_secret_value(),
            }
        )
        try:
            tools = ChatValidationTools(pool)
            request_id = "chat-check-fixture-tool-call-1"
            started = tools.request_check(program_id, version_id, request_id)
            duplicate = tools.request_check(program_id, version_id, request_id)
            assert duplicate.check_job_id == started.check_job_id
            assert started.version_id == version_id
            assert started.input_version_id == version_id
            assert started.status in {"pending", "completed"}

            deadline = time.monotonic() + 90
            result = started
            while result.status == "pending" and time.monotonic() < deadline:
                time.sleep(0.5)
                result = tools.read_check(program_id, version_id, started.check_job_id)

            assert result.status == "completed", result.model_dump(mode="json")
            assert result.check_job_id == started.check_job_id
            assert result.input_version_id == version_id
            assert result.output_version_id is None
            assert result.coverage

            response = http.get(root + f"/reviews/{review['id']}/checks/{started.check_job_id}")
            assert response.status_code == 200, response.text
            api_result = response.json()
            assert api_result["status"] == "completed"
            assert api_result["input_version_id"] == str(version_id)
            assert api_result["report"]["coverage"]

            with pool.connection() as connection:
                connection.execute(
                    "UPDATE processing_jobs SET report=%s WHERE id=%s",
                    (
                        Jsonb(
                            {
                                "outcome": "failed",
                                "code": "TEST_RETRY",
                                "message": "Тестовая ошибка проверки",
                            }
                        ),
                        started.check_job_id,
                    ),
                )
            failed = tools.read_check(program_id, version_id, started.check_job_id)
            assert failed.status == "failed"
            same_failed_call = tools.request_check(program_id, version_id, request_id)
            assert same_failed_call.check_job_id == started.check_job_id
            assert same_failed_call.status == "failed"

            retried = tools.request_check(program_id, version_id, "chat-check-fixture-tool-call-2")
            assert retried.check_job_id != started.check_job_id
            assert retried.input_version_id == version_id
            assert retried.status in {"pending", "completed"}
            retry_deadline = time.monotonic() + 90
            retry_result = retried
            while retry_result.status == "pending" and time.monotonic() < retry_deadline:
                time.sleep(0.5)
                retry_result = tools.read_check(program_id, version_id, retried.check_job_id)
            assert retry_result.status == "completed", retry_result.model_dump(mode="json")
            with pool.connection() as connection:
                count = connection.execute(
                    "SELECT count(*) FROM processing_jobs WHERE program_id=%s "
                    "AND type='validate' AND parameters->>'purpose'='check' "
                    "AND input_version_id=%s AND operation_key LIKE %s",
                    (program_id, version_id, "chat-agent-check:%"),
                ).fetchone()[0]
            assert count == 2
        finally:
            pool.close()
