"""Проверки ограничений входа и данных инструментов чата."""

import asyncio
import json
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from uuid import uuid4

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import ValidationError
from temporalio.client import WorkflowExecutionStatus

import app.chat_api as chat_api
import worker.agent.read_adapter as read_adapter
from app.chat_api import MessageCreate, router
from app.chat_dispatcher import dispatch_chats
from app.domain import Entity, SnapshotV2, SourceRefV2
from worker.agent.read_adapter import PostgresChatReadPort


def test_chat_message_rejects_nul_and_normalizes_whitespace():
    with pytest.raises(ValidationError):
        MessageCreate(text="Вопрос\x00")

    assert MessageCreate(text="  Вопрос  ").text == "Вопрос"


def test_entity_search_tool_payload_is_bounded():
    adapter = PostgresChatReadPort(None)
    adapter.snapshot = lambda *_: SnapshotV2(
        entities=[
            Entity(
                id=uuid4(),
                kind="discipline",
                attributes={"name": "Алгебра", "body": "x" * (3 * 1024 * 1024)},
            )
        ]
    )

    result = asyncio.run(
        adapter.search_entities(
            program_id=uuid4(), version_id=uuid4(), query="Алгебра", entity_kinds=None, limit=20
        )
    )

    assert len(result.hits) == 1
    assert len(result.model_dump_json().encode()) < 4096


def test_source_fragment_bounds_source_reference(tmp_path, monkeypatch):
    artifact_id, document_id, source_id = uuid4(), uuid4(), uuid4()
    (tmp_path / str(artifact_id)).write_text("Источник", encoding="utf-8")

    class Cursor:
        def __init__(self, row):
            self.row = row

        def __enter__(self):
            return self

        def __exit__(self, *_):
            return None

        def execute(self, query, _params):
            self.row = (str(artifact_id),) if "storage_key" in query else ("source.docx",)
            return self

        def fetchone(self):
            return self.row

    class Pool:
        def connection(self):
            return Cursor(None)

    monkeypatch.setattr(
        read_adapter,
        "get_settings",
        lambda: SimpleNamespace(artifact_files_path=tmp_path),
    )
    port = PostgresChatReadPort(Pool())
    port.snapshot = lambda *_: SnapshotV2(
        document_ids=[document_id],
        source_refs=[
            SourceRefV2(
                id=source_id,
                document_id=document_id,
                locator="x" * (3 * 1024 * 1024),
                artifact_id=artifact_id,
                artifact_locator="line:1",
            )
        ],
    )

    result = asyncio.run(
        port.read_source_fragment(
            program_id=uuid4(),
            version_id=uuid4(),
            source_ref_id=source_id,
            context_paragraphs=0,
        )
    )

    assert len(result.model_dump_json().encode()) < 4096


def test_sse_serializes_events_and_rechecks_completed_state(monkeypatch):
    program_id, session_id, run_id = uuid4(), uuid4(), uuid4()

    class Repository:
        reads = 0

        def turn_input(self, _):
            return {"program_id": program_id, "session_id": session_id}

        def session(self, *_):
            self.reads += 1
            status = "pending" if self.reads == 1 else "completed"
            return {
                "turns": [
                    {
                        "id": run_id,
                        "status": status,
                        "result": {"kind": "answer", "message": "Готово", "sources": []},
                    }
                ]
            }

        def fail(self, _):
            raise AssertionError("Успешный ход нельзя помечать как failed")

    class Handle:
        async def describe(self):
            return SimpleNamespace(status=WorkflowExecutionStatus.COMPLETED)

    repository = Repository()
    monkeypatch.setattr(chat_api, "repo", lambda _: repository)
    app = FastAPI()
    app.include_router(router)
    app.state.resources = SimpleNamespace(
        temporal=SimpleNamespace(get_workflow_handle=lambda _: Handle())
    )

    with TestClient(app) as client:
        response = client.get(
            f"/api/v1/programs/{program_id}/chat/runs/{run_id}/events"
        )

    assert response.status_code == 200
    assert "event: completed" in response.text
    data = next(
        line.removeprefix("data: ")
        for line in response.text.splitlines()
        if line.startswith("data: ")
    )
    assert json.loads(data)["message"] == "Готово"
    assert "event: failed" not in response.text


def test_dispatcher_pages_past_one_hundred_pending_turns():
    records = [
        (uuid4(), f"chat-{index}", datetime(2026, 1, 1, tzinfo=UTC) + timedelta(microseconds=index))
        for index in range(101)
    ]

    class Repository:
        def pending(self, after=None, limit=100):
            remaining = [row for row in records if after is None or (row[2], row[0]) > after]
            return [
                (identity, workflow_id, created_at)
                for identity, workflow_id, created_at in remaining[:limit]
            ]

        def turn_input(self, turn_id):
            return {
                "user_text": "Вопрос",
                "program_id": uuid4(),
                "version_id": uuid4(),
                "conversation": None,
            }

    class Client:
        started = []

        async def start_workflow(self, *_args, **kwargs):
            self.started.append(kwargs["id"])

    class Stop:
        checks = 0

        def is_set(self):
            self.checks += 1
            return self.checks > 1

        async def wait(self):
            return None

    client = Client()
    asyncio.run(dispatch_chats(client, Repository(), "audit", Stop()))

    assert len(client.started) == 101
