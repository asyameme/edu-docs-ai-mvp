"""Контракт и быстрая сериализация ответов решений сверки."""

import json
from datetime import UTC, datetime
from types import SimpleNamespace
from uuid import uuid4

from fastapi import FastAPI
from fastapi.responses import Response
from fastapi.testclient import TestClient

import app.review_api as review_api
from app.domain import VersionRead
from app.review_contract import ConfirmCreate, DecisionBatchCreate, DecisionCreate


def version_value():
    version_id, program_id, parent_id = uuid4(), uuid4(), uuid4()
    document_id, entity_id, reference_id, link_id = [uuid4() for _ in range(4)]
    return {
        "id": version_id,
        "program_id": program_id,
        "number": 2,
        "parent_version_id": parent_id,
        "restored_from_version_id": None,
        "origin": "save",
        "created_at": datetime.now(UTC),
        "snapshot": {
            "schema_version": 2,
            "metadata": {"probe": True, "count": 0, "fraction": 0.75},
            "document_ids": [document_id],
            "entities": [
                {
                    "id": entity_id,
                    "kind": "discipline",
                    "attributes": {"value": None, "text": "ё"},
                    "source_ref_ids": [reference_id],
                }
            ],
            "relations": [],
            "source_refs": [
                {
                    "id": reference_id,
                    "document_id": document_id,
                    "section": None,
                    "locator": "file",
                    "artifact_id": None,
                    "artifact_locator": None,
                }
            ],
            "source_links": [
                {
                    "id": link_id,
                    "target_kind": "entity",
                    "target_id": entity_id,
                    "field_path": "/attributes/value",
                    "source_ref_id": reference_id,
                    "observed_value": {"value": None, "text": "ё"},
                }
            ],
            "review": None,
        },
    }


def test_version_routes_return_prevalidated_json_with_unchanged_openapi(monkeypatch):
    value = version_value()

    class Repository:
        def __init__(self, _pool):
            pass

        def decide(self, *args):
            return value

        def decide_many(self, *args):
            return value

        def confirm(self, *args):
            return value

    monkeypatch.setattr(review_api, "ReviewRepository", Repository)
    program_id, review_id, version_id, conflict_id, variant_id, check_id = [
        uuid4() for _ in range(6)
    ]
    operation_key = "test-key"
    repo = SimpleNamespace(pool=None)
    decision = DecisionCreate(
        expected_version_id=version_id,
        conflict_id=conflict_id,
        variant_id=variant_id,
        signature="Проверка",
    )
    batch = DecisionBatchCreate(
        expected_version_id=version_id,
        selections=[{"conflict_id": conflict_id, "variant_id": variant_id}],
        signature="Проверка",
    )
    confirm = ConfirmCreate(
        expected_version_id=version_id,
        check_job_id=check_id,
        signature="Проверка",
    )
    responses = [
        review_api.decide(program_id, review_id, decision, operation_key, repo),
        review_api.decide_batch(program_id, review_id, batch, operation_key, repo),
        review_api.confirm(program_id, review_id, confirm, operation_key, repo),
    ]
    expected = VersionRead.model_validate(value).model_dump(mode="json")
    assert all(isinstance(response, Response) for response in responses)
    assert all(json.loads(response.body) == expected for response in responses)

    app = FastAPI()
    app.state.resources = SimpleNamespace(postgres=None)
    app.include_router(review_api.router)
    schema = app.openapi()
    for path in (
        "/api/v1/programs/{program_id}/reviews/{review_id}/decisions",
        "/api/v1/programs/{program_id}/reviews/{review_id}/decisions/batch",
        "/api/v1/programs/{program_id}/reviews/{review_id}/confirm",
    ):
        assert schema["paths"][path]["post"]["responses"]["201"]["content"][
            "application/json"
        ]["schema"]["$ref"] == "#/components/schemas/VersionRead"

    with TestClient(app) as client:
        response = client.post(
            f"/api/v1/programs/{program_id}/reviews/{review_id}/decisions",
            json={
                "expected_version_id": str(version_id),
                "conflict_id": str(conflict_id),
                "variant_id": str(variant_id),
                "signature": "Проверка",
            },
            headers={"Idempotency-Key": operation_key},
        )
    assert response.status_code == 201
    assert response.headers["content-type"] == "application/json"
    assert response.json() == expected
