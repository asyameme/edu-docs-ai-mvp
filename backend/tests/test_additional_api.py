"""Ошибки, которые не должны повреждать сохранённые оригиналы и версии."""

import os
from uuid import uuid4

import httpx
import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.repository import Repository

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(os.getenv("RUN_INTEGRATION") != "1", reason="Needs isolated stack"),
]


def test_nonfinite_json_is_rejected_without_creating_version():
    with httpx.Client(base_url="http://frontend", timeout=10, trust_env=False) as http:
        program = http.post("/api/v1/programs", json={"name": "Nonfinite acceptance"}).json()
        base = "/api/v1/programs/" + program["id"]
        body = (
            '{"expected_version_id":"'
            + program["current_version_id"]
            + '","snapshot":{"metadata":{"value":NaN}}}'
        )
        response = http.post(
            base + "/versions",
            content=body,
            headers={"Content-Type": "application/json", "Idempotency-Key": str(uuid4())},
        )
        assert response.status_code == 422, response.text
        assert len(http.get(base + "/versions").json()) == 1


def test_nul_strings_return_422_without_changing_persisted_state():
    with httpx.Client(base_url="http://frontend", timeout=10, trust_env=False) as http:
        before = http.get("/api/v1/programs", params={"limit": 100}).json()
        response = http.post("/api/v1/programs", json={"name": "bad\x00name"})
        assert response.status_code == 422, response.text
        assert http.get("/api/v1/programs", params={"limit": 100}).json() == before
        program = http.post("/api/v1/programs", json={"name": "NUL acceptance"}).json()
        base = "/api/v1/programs/" + program["id"]
        for metadata in ({"note": "bad\x00value"}, {"bad\x00key": "value"},
                         {"nested": [{"note": "bad\x00value"}]}):
            response = http.post(
                base + "/versions",
                json={"expected_version_id": program["current_version_id"],
                      "snapshot": {"metadata": metadata}},
                headers={"Idempotency-Key": str(uuid4())},
            )
            assert response.status_code == 422, response.text
        assert len(http.get(base + "/versions").json()) == 1
        assert http.get(base).json()["current_version_id"] == program["current_version_id"]


def test_lost_commit_acknowledgement_does_not_delete_committed_original(monkeypatch):
    import psycopg

    real_add = Repository.add_document

    def commit_then_disconnect(self, *args):
        real_add(self, *args)
        raise psycopg.OperationalError("Injected lost commit acknowledgement: private-password")

    with TestClient(app) as client:
        program = client.post("/api/v1/programs", json={"name": "Lost acknowledgement"}).json()
        base = "/api/v1/programs/" + program["id"]
        monkeypatch.setattr(Repository, "add_document", commit_then_disconnect)
        response = client.post(base + "/documents", files={"file": ("test.pdf", b"original bytes")})
        assert response.status_code == 503
        assert "private-password" not in response.text
        docs = client.get(base + "/documents").json()
        assert len(docs) == 1
        result = client.get(base + "/documents/" + docs[0]["id"] + "/download")
        assert result.status_code == 200
        assert result.content == b"original bytes"


def test_restore_repeated_key_returns_same_version_and_other_request_conflicts():
    with httpx.Client(base_url="http://frontend", timeout=10, trust_env=False) as http:
        program = http.post("/api/v1/programs", json={"name": "Restore idempotency"}).json()
        base = "/api/v1/programs/" + program["id"]
        result = http.post(
            base + "/versions",
            json={
                "expected_version_id": program["current_version_id"],
                "snapshot": {"metadata": {"edited": True}},
            },
            headers={"Idempotency-Key": str(uuid4())},
        )
        assert result.status_code == 201
        v2 = result.json()
        key = str(uuid4())
        restore_url = base + "/versions/" + program["current_version_id"] + "/restore"
        request = {"expected_version_id": v2["id"]}
        first = http.post(restore_url, json=request, headers={"Idempotency-Key": key})
        assert first.status_code == 201, first.text
        repeat = http.post(restore_url, json=request, headers={"Idempotency-Key": key})
        assert repeat.status_code == 201
        assert repeat.json() == first.json()
        changed = {"expected_version_id": first.json()["id"]}
        assert (
            http.post(restore_url, json=changed, headers={"Idempotency-Key": key}).status_code
            == 409
        )
        assert len(http.get(base + "/versions").json()) == 3
