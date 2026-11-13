"""Публичный контракт чата: ошибки до SSE, история и схемы OpenAPI."""

from datetime import UTC, datetime
from types import SimpleNamespace
from typing import get_args
from uuid import uuid4

import pytest
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient
from pydantic import TypeAdapter

import app.chat_api as chat_api
from app.domain import Entity
from app.errors import DomainError
from worker.agent.read_port import EntityKind


def client_for(monkeypatch, repository):
    monkeypatch.setattr(chat_api, "repo", lambda _: repository)
    app = FastAPI()
    app.include_router(chat_api.router)

    @app.exception_handler(DomainError)
    async def handle_domain_error(request: Request, error: DomainError):
        return JSONResponse(status_code=error.status, content={"detail": error.detail})

    return TestClient(app)


@pytest.mark.parametrize("case,status", [("missing", 404), ("foreign", 404), ("invalid", 422)])
def test_sse_validates_run_before_streaming(monkeypatch, case, status):
    class Repository:
        def turn_input(self, _):
            if case == "missing":
                raise DomainError(404, "Ход чата не найден")
            return {"program_id": uuid4(), "session_id": uuid4()}

    run_id = "invalid" if case == "invalid" else str(uuid4())
    with client_for(monkeypatch, Repository()) as client:
        response = client.get(f"/api/v1/programs/{uuid4()}/chat/runs/{run_id}/events")

    assert response.status_code == status
    assert response.headers["content-type"].startswith("application/json")
    assert "detail" in response.json()


def test_search_filter_accepts_every_graph_entity_kind():
    graph_kinds = set(get_args(Entity.model_fields["kind"].annotation))
    assert set(get_args(EntityKind)) == graph_kinds
    for kind in graph_kinds:
        assert TypeAdapter(EntityKind).validate_python(kind) == kind


@pytest.mark.parametrize(
    "result",
    [
        None,
        {"kind": "answer", "message": "Ответ", "sources": []},
        {"kind": "clarification", "message": "Уточните", "questions": ["Что найти?"]},
        {
            "kind": "change_proposal",
            "summary": "Предложение",
            "changes": [
                {
                    "target_kind": "entity",
                    "target_id": str(uuid4()),
                    "field_path": "/attributes/name",
                    "expected_old_value": "А",
                    "new_value": "Б",
                    "reason": "Обсуждение",
                    "source_ref_ids": [str(uuid4())],
                }
            ],
        },
    ],
)
def test_history_has_typed_result_and_hides_internal_conversation(monkeypatch, result):
    program_id, session_id, turn_id, version_id = uuid4(), uuid4(), uuid4(), uuid4()
    now = datetime.now(UTC)

    class Repository:
        def session(self, *_):
            return {
                "id": session_id,
                "program_id": program_id,
                "version_id": version_id,
                "title": "",
                "created_at": now,
                "updated_at": now,
                "conversation": "private",
                "turns": [
                    {
                        "id": turn_id,
                        "run_id": turn_id,
                        "status": "completed" if result else "pending",
                        "user_text": "Вопрос",
                        "result": result,
                        "created_at": now,
                        "completed_at": now if result else None,
                        "conversation": "private",
                    }
                ],
            }

    with client_for(monkeypatch, Repository()) as client:
        response = client.get(f"/api/v1/programs/{program_id}/chat/sessions/{session_id}")
        schema = client.get("/openapi.json").json()

    assert response.status_code == 200
    body = response.json()
    assert "conversation" not in body
    assert "conversation" not in body["turns"][0]
    if result is None:
        assert body["turns"][0]["result"] is None
        assert body["turns"][0]["completed_at"] is None
    else:
        assert body["turns"][0]["result"]["kind"] == result["kind"]
    assert body["turns"][0]["id"] == body["turns"][0]["run_id"] == str(turn_id)
    history_schema = schema["paths"]["/api/v1/programs/{program_id}/chat/sessions/{session_id}"][
        "get"
    ]["responses"]["200"]["content"]["application/json"]["schema"]
    assert "$ref" in history_schema
    assert set(schema["components"]["schemas"]["TurnRead"]["properties"]["status"]["enum"]) == {
        "pending",
        "completed",
        "failed",
    }
    events_schema = schema["paths"]["/api/v1/programs/{program_id}/chat/runs/{run_id}/events"][
        "get"
    ]["responses"]["200"]["content"]["text/event-stream"]["schema"]
    assert events_schema["discriminator"]["propertyName"] == "event"


@pytest.mark.parametrize(
    "case,expected",
    [
        ("disabled", 503),
        ("conflict", 409),
        ("empty", 422),
        ("nul", 422),
        ("missing-key", 422),
        ("accepted", 202),
    ],
)
def test_message_http_contract(monkeypatch, case, expected):
    turn_id = uuid4()

    class Repository:
        def create_turn(self, *_):
            if case == "conflict":
                raise DomainError(409, "Ключ использован для другого сообщения")
            return turn_id

        def turn_status(self, _):
            return {"id": turn_id, "status": "pending"}

    monkeypatch.setattr(
        chat_api,
        "get_settings",
        lambda: SimpleNamespace(
            chat_api_ready=case != "disabled"
        ),
    )
    text = " " if case == "empty" else "Вопрос\x00" if case == "nul" else "Вопрос"
    headers = {} if case == "missing-key" else {"Idempotency-Key": "test"}
    with client_for(monkeypatch, Repository()) as client:
        response = client.post(
            f"/api/v1/programs/{uuid4()}/chat/sessions/{uuid4()}/messages",
            headers=headers,
            json={"text": text},
        )
    assert response.status_code == expected
    if expected == 202:
        assert response.json() == {"id": str(turn_id), "run_id": str(turn_id), "status": "pending"}
    else:
        assert "detail" in response.json()


def test_sse_failed_event_keeps_public_shape(monkeypatch):
    program_id, run_id = uuid4(), uuid4()

    class Repository:
        def turn_input(self, _):
            return {"program_id": program_id, "session_id": uuid4()}

        def session(self, *_):
            return {"turns": [{"id": run_id, "status": "failed", "result": None}]}

    with client_for(monkeypatch, Repository()) as client:
        response = client.get(f"/api/v1/programs/{program_id}/chat/runs/{run_id}/events")
    assert response.status_code == 200
    assert "event: failed" in response.text
    assert str(run_id) in response.text
    assert '"message":' in response.text
