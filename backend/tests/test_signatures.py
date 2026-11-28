"""Подпись обязательна для пользовательских действий и сохраняется в журнале БД."""

import os
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from app.domain import VersionCreate, VersionRestore
from app.main import app
from app.review_contract import DecisionCreate


@pytest.mark.parametrize("model", [VersionCreate, VersionRestore, DecisionCreate])
@pytest.mark.parametrize("signature", [None, "", "   ", "я" * 121])
def test_user_action_rejects_missing_or_invalid_signature(model, signature):
    body = {
        "expected_version_id": uuid4(),
        "snapshot": {},
        "conflict_id": uuid4(),
        "variant_id": uuid4(),
        "check_job_id": uuid4(),
    }
    body = {key: value for key, value in body.items() if key in model.model_fields}
    if signature is not None:
        body["signature"] = signature
    with pytest.raises(ValidationError, match="signature"):
        model.model_validate(body)


@pytest.mark.integration
@pytest.mark.skipif(os.getenv("RUN_INTEGRATION") != "1", reason="Нужна тестовая PostgreSQL")
def test_save_restore_preserve_signature_and_reject_changed_signature_on_retry():
    with TestClient(app) as client:
        program = client.post("/api/v1/programs", json={"name": "Подпись версии"}).json()
        root = "/api/v1/programs/" + program["id"]
        body = {
            "expected_version_id": program["current_version_id"],
            "snapshot": {"metadata": {"hours": 144}},
            "signature": "  Анна  ",
        }
        headers = {"Idempotency-Key": uuid4().hex}
        saved = client.post(root + "/versions", json=body, headers=headers)
        assert saved.status_code == 201, saved.text
        assert client.post(root + "/versions", json=body, headers=headers).json() == saved.json()
        body["signature"] = "Борис"
        assert client.post(root + "/versions", json=body, headers=headers).status_code == 409
        restored = client.post(
            root + "/versions/" + program["current_version_id"] + "/restore",
            json={"expected_version_id": saved.json()["id"], "signature": "  Борис  "},
            headers={"Idempotency-Key": uuid4().hex},
        )
        assert restored.status_code == 201, restored.text
        assert client.put(root + "/signature", json={"value": "Вера"}).status_code == 200
        with app.state.resources.postgres.connection() as conn:
            for version, author in ((saved.json(), "Анна"), (restored.json(), "Борис")):
                rows = conn.execute(
                    "SELECT actor_type,actor_id FROM change_log WHERE version_id=%s",
                    (version["id"],),
                ).fetchall()
                assert rows
                assert all(tuple(row) == ("user", author) for row in rows)
        assert len(client.get(root + "/versions").json()) == 3


@pytest.mark.integration
@pytest.mark.skipif(os.getenv("RUN_INTEGRATION") != "1", reason="Нужна тестовая PostgreSQL")
def test_program_signature_survives_reopening_and_is_scoped_to_program():
    with TestClient(app) as client:
        first = client.post("/api/v1/programs", json={"name": "Подпись"}).json()
        second = client.post("/api/v1/programs", json={"name": "Другая программа"}).json()
        root = "/api/v1/programs/" + first["id"]
        assert first["signature"] is None
        saved = client.put(root + "/signature", json={"value": "  Анна  "})
        assert saved.status_code == 200, saved.text
        assert saved.json()["signature"] == "Анна"
        assert saved.json()["current_version_id"] == first["current_version_id"]
    with TestClient(app) as reopened:
        assert reopened.get(root).json()["signature"] == "Анна"
        assert reopened.get("/api/v1/programs/" + second["id"]).json()["signature"] is None
        assert reopened.put(root + "/signature", json={"value": "Борис"}).status_code == 200
        assert reopened.get(root).json()["signature"] == "Борис"
        for body in ({}, {"value": None}, {"value": ""}, {"value": " \t\n"},
                     {"value": "я" * 121}, {"value": "a\x00b"}):
            assert reopened.put(root + "/signature", json=body).status_code == 422
        assert reopened.get(root).json()["signature"] == "Борис"
        for value in ("😀" * 120, "  " + "я" * 120 + "  "):
            assert reopened.put(root + "/signature", json={"value": value}).status_code == 200
            assert reopened.get(root).json()["signature"] == value.strip()
        assert reopened.put("/api/v1/programs/" + str(uuid4()) + "/signature",
                            json={"value": "Анна"}).status_code == 404
