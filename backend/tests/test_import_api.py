"""Импорт пакета через настоящий HTTP; только отдельный тестовый Compose-проект."""

import io
import os
import time
import zipfile
from uuid import uuid4

import httpx
import pytest

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(os.getenv("RUN_INTEGRATION") != "1", reason="Needs isolated services"),
]


def package(files):
    stream = io.BytesIO()
    with zipfile.ZipFile(stream, "w") as archive:
        for name, content in files.items():
            archive.writestr(name, content)
    return stream.getvalue()


def test_import_is_persisted_and_duplicate_reuses_job():
    with httpx.Client(base_url="http://frontend", timeout=30, trust_env=False) as http:
        program = http.post("/api/v1/programs", json={"name": "Проверка импорта"}).json()
        path = f"/api/v1/programs/{program['id']}/imports"
        content = package({"test.docx": b"artificial document for unavailable parser"})
        key = uuid4().hex
        response = http.post(
            path, files={"file": ("package.zip", content)}, headers={"Idempotency-Key": key}
        )
        assert response.status_code == 202, response.text
        job = response.json()
        assert job["status"] in {"pending", "processing", "failed"}
        repeated = http.post(
            path,
            files={"file": ("renamed.zip", content)},
            headers={"Idempotency-Key": uuid4().hex},
        )
        assert repeated.status_code == 202, repeated.text
        assert repeated.json()["id"] == job["id"]
        assert len(http.get(f"/api/v1/programs/{program['id']}/documents").json()) == 1
        deadline = time.monotonic() + 40
        while time.monotonic() < deadline:
            result = http.get(path + "/" + job["id"])
            assert result.status_code == 200
            state = result.json()
            if state["status"] in {"completed", "partial", "failed"}:
                break
            time.sleep(0.5)
        assert state["status"] == "failed", state
        assert state["files"][0]["error_code"] == "parser_not_configured"
        assert http.get(path + "/" + str(uuid4())).status_code == 404


@pytest.mark.parametrize("files", [{"empty.docx": b""}, {"../escape.xlsx": b"x"}])
def test_invalid_package_does_not_save_documents(files):
    with httpx.Client(base_url="http://frontend", timeout=30, trust_env=False) as http:
        program = http.post("/api/v1/programs", json={"name": "Некорректный архив"}).json()
        response = http.post(
            f"/api/v1/programs/{program['id']}/imports",
            files={"file": ("package.zip", package(files))},
            headers={"Idempotency-Key": uuid4().hex},
        )
        assert response.status_code == 422, response.text
        assert http.get(f"/api/v1/programs/{program['id']}/documents").json() == []
