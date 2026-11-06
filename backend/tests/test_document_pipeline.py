"""Формат V2 через HTTP/Temporal/SQL с искусственным поставщиком JSON."""

import json
import os
from hashlib import sha256
from uuid import UUID, uuid4

import httpx
import pytest
from document_cases import payload
from fastapi.testclient import TestClient
from psycopg.rows import dict_row
from test_import_processing import archive, wait

from app.domain import SnapshotV2
from app.main import app
from app.repository import Repository
from app.review_logic import copy_sources
from app.review_storage import insert_version

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        os.getenv("PARSER_FIXTURE_TESTS") != "1", reason="Нужен тестовый HTTP-сервер"
    ),
]


@pytest.mark.parametrize("kind", ["RPD", "RPP", "CURRICULUM", "MATRIX", "CKMV", "CHARACTERISTIC"])
def test_v2_document_reaches_import_result_and_relational_history(kind):
    original = b"contract-v2:" + json.dumps(payload(kind), ensure_ascii=False).encode()
    with httpx.Client(base_url="http://frontend", timeout=30, trust_env=False) as http:
        program = http.post("/api/v1/programs", json={"name": "Документ V2"}).json()
        root = f"/api/v1/programs/{program['id']}"
        accepted = http.post(
            root + "/imports",
            files={"file": ("data.zip", archive({"source.docx": original}))},
            headers={"Idempotency-Key": uuid4().hex},
        )
        assert accepted.status_code == 202, accepted.text
        path = root + "/imports/" + accepted.json()["id"]
        job = wait(http, path)
        assert job["status"] == "completed", job
        response = http.get(path + "/result")
        assert response.status_code == 200, response.text
        snapshot = SnapshotV2.model_validate(response.json())
        document_id = job["files"][0]["document_id"]
        parsed = snapshot.metadata["parsed_documents"][document_id]
        assert parsed["meta"]["schema_version"] == 2
        assert parsed["meta"]["file_hash"] == sha256(original).hexdigest()
        assert parsed["meta"]["program_id"] == "26/5003/1"
        assert http.get(root + f"/documents/{document_id}/download").content == original
    with TestClient(app):
        repo = Repository(app.state.resources.postgres)
        version_id = uuid4()
        snapshot = copy_sources(snapshot, version_id)
        with repo.pool.connection() as connection, connection.cursor(row_factory=dict_row) as cur:
            insert_version(
                cur, UUID(program["id"]), UUID(program["current_version_id"]), version_id, snapshot
            )
        restored = repo.save(UUID(program["id"]), version_id, uuid4().hex, restore_id=version_id)
        for vid in (version_id, restored["id"]):
            stored = repo.version(UUID(program["id"]), vid)["snapshot"]
            assert stored["metadata"]["parsed_documents"][document_id] == parsed
            with repo.pool.connection() as connection:
                assert connection.execute(
                    "SELECT count(*) FROM entities WHERE version_id=%s", (vid,)
                ).fetchone()[0] == len(snapshot.entities)
                assert connection.execute(
                    "SELECT count(*) FROM source_links WHERE version_id=%s", (vid,)
                ).fetchone()[0] == len(snapshot.source_links)
                if kind == "MATRIX":
                    assert sorted(
                        row[0]
                        for row in connection.execute(
                            "SELECT matrix_value FROM relations WHERE version_id=%s", (vid,)
                        )
                    ) == [0, 1]


def test_v2_package_keeps_documents_without_merging_observations():
    original = b"contract-v2:" + json.dumps(payload(), ensure_ascii=False).encode()
    with httpx.Client(base_url="http://frontend", timeout=30, trust_env=False) as http:
        program = http.post("/api/v1/programs", json={"name": "Смешанный пакет"}).json()
        root = f"/api/v1/programs/{program['id']}"
        accepted = http.post(
            root + "/imports",
            files={"file": ("data.zip", archive({"old.docx": b"valid", "new.docx": original}))},
            headers={"Idempotency-Key": uuid4().hex},
        )
        assert accepted.status_code == 202, accepted.text
        path = root + "/imports/" + accepted.json()["id"]
        job = wait(http, path)
        assert job["status"] == "completed", job
        state = http.get(path + "/result")
        assert state.status_code == 200, state.text
        result = SnapshotV2.model_validate(state.json())
        assert len(result.metadata["parsed_documents"]) == 2
        assert len([e for e in result.entities if e.kind == "discipline"]) == 2


@pytest.mark.parametrize("invalid", ["boolean_cell", "unknown_version", "legacy_version"])
def test_invalid_v2_result_is_rejected_before_saving_parsed_output(invalid):
    data = payload("MATRIX")
    if invalid == "boolean_cell":
        data["matrix_cells"][0]["value"] = False
    elif invalid == "legacy_version":
        data["meta"]["schema_version"] = 1
    else:
        data["meta"]["schema_version"] = 3
    original = b"contract-v2:" + json.dumps(data, ensure_ascii=False).encode()
    with httpx.Client(base_url="http://frontend", timeout=30, trust_env=False) as http:
        program = http.post("/api/v1/programs", json={"name": "Некорректный результат"}).json()
        root = f"/api/v1/programs/{program['id']}"
        accepted = http.post(
            root + "/imports",
            files={"file": ("data.zip", archive({"bad.xlsx": original}))},
            headers={"Idempotency-Key": uuid4().hex},
        )
        assert accepted.status_code == 202, accepted.text
        path = root + "/imports/" + accepted.json()["id"]
        job = wait(http, path)
        assert job["status"] == "failed", job
        assert job["files"][0]["error_code"] == "parser_invalid_result"
        assert http.get(path + "/result").status_code == 409
        assert len(http.get(root + "/documents").json()) == 1


@pytest.mark.parametrize("explicit_version", [True, False])
def test_v2_coordinate_allowed_by_main_survives_http_without_conversion(explicit_version):
    data = payload()
    data["disciplines"][0]["source_ref"]["paragraph_index"] = 0
    if not explicit_version:
        data["meta"].pop("schema_version")
    original = b"contract-v2:" + json.dumps(data, ensure_ascii=False).encode()
    with httpx.Client(base_url="http://frontend", timeout=30, trust_env=False) as http:
        program = http.post("/api/v1/programs", json={"name": "Координаты общего контракта"}).json()
        root = f"/api/v1/programs/{program['id']}"
        accepted = http.post(
            root + "/imports",
            files={"file": ("data.zip", archive({"source.docx": original}))},
            headers={"Idempotency-Key": uuid4().hex},
        )
        assert accepted.status_code == 202, accepted.text
        path = root + "/imports/" + accepted.json()["id"]
        job = wait(http, path)
        assert job["status"] == "completed", job
        response = http.get(path + "/result")
        assert response.status_code == 200, response.text
        snapshot = SnapshotV2.model_validate(response.json())
        parsed = snapshot.metadata["parsed_documents"][job["files"][0]["document_id"]]
        assert parsed["disciplines"][0]["source_ref"]["paragraph_index"] == 0
        assert {json.loads(ref.locator)["paragraph_index"] for ref in snapshot.source_refs} == {0}
