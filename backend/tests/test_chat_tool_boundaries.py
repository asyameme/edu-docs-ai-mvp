"""Общие границы выдачи tools, конфигурации и доступности найденного текста."""

import asyncio
import inspect
import os
import subprocess
import sys
from uuid import uuid4

import pytest
from pydantic_ai import ModelRetry
from test_chat_http_contract import client_for

import app.chat_api as api
from app.config import Settings
from app.domain import Entity, RelationV2, SnapshotV2
from worker.agent.read_adapter import PostgresChatReadPort
from worker.agent.read_port import ChatReadPort


def test_every_read_tool_uses_the_serialized_payload_guard():
    for name, method in ChatReadPort.__dict__.items():
        if inspect.iscoroutinefunction(method):
            assert getattr(getattr(PostgresChatReadPort, name), "__payload_limited__", False)


@pytest.mark.parametrize(
    "model,url,key,flag,api_ready,worker_ready",
    [
        ("", "https://example.invalid/v1", "key", False, False, False),
        ("test", "", "key", False, False, False),
        ("test", "https://example.invalid/v1", "", False, False, False),
        ("test", "https://example.invalid/v1", "key", False, True, True),
        ("test", "https://example.invalid/v1", "", True, True, False),
    ],
)
def test_shared_configuration_matrix(model, url, key, flag, api_ready, worker_ready):
    settings = Settings(
        _env_file=None,
        postgres_password="test",
        chat_enabled=True,
        chat_model=model,
        chat_base_url=url,
        chat_api_key=key,
        chat_credentials_configured=flag,
    )
    assert settings.chat_api_ready is api_ready
    if worker_ready:
        assert settings.validate_chat_worker_config()
    else:
        with pytest.raises(ValueError, match="Неполная настройка чата"):
            settings.validate_chat_worker_config()


@pytest.mark.parametrize(
    "tool", ["read_entity", "read_graph_projection", "get_validation_findings"]
)
def test_large_metadata_cannot_leave_tool_boundary(tool):
    first, second = uuid4(), uuid4()
    large = "x" * (3 * 1024 * 1024)

    class Connection:
        def __enter__(self):
            return self

        def __exit__(self, *_):
            return None

        def execute(self, *_):
            return self

        def fetchall(self):
            return [(uuid4(), large, large, "warning", "open", {})]

    class Pool:
        def connection(self):
            return Connection()

    port = PostgresChatReadPort(Pool())
    port.snapshot = lambda *_: SnapshotV2(
        entities=[Entity(id=first, kind="discipline"), Entity(id=second, kind="competency")],
        relations=[RelationV2(id=uuid4(), source_id=first, target_id=second, type=large)],
    )
    kwargs = {"program_id": uuid4(), "version_id": uuid4()}
    if tool == "get_validation_findings":
        kwargs["limit"] = 1
    else:
        kwargs["entity_id"] = first
    if tool == "read_graph_projection":
        kwargs["depth"] = 1
    with pytest.raises(ModelRetry) as caught:
        asyncio.run(getattr(port, tool)(**kwargs))
    assert len(str(caught.value).encode()) < 1024
    assert large not in str(caught.value)


@pytest.mark.parametrize(
    "attributes,query",
    [
        ({"text": "начало " * 300 + "контрольэкзамен"}, "контрольэкзамен"),
        (
            {**{f"key{i}": "начало " * 40 for i in range(13)}, "last": "контрольэкзамен"},
            "контрольэкзамен",
        ),
        ({"nested": {"text": "начало " * 300 + "контрольэкзамен"}}, "контрольэкзамен"),
        ({"text": "ß" * 1000 + "Straße"}, "STRASSE"),
    ],
)
def test_matching_context_remains_available(attributes, query):
    identity = uuid4()
    port = PostgresChatReadPort(None)
    port.snapshot = lambda *_: SnapshotV2(
        entities=[Entity(id=identity, kind="document_section", attributes=attributes)]
    )
    result = asyncio.run(
        port.search_entities(
            program_id=uuid4(), version_id=uuid4(), query=query, entity_kinds=None, limit=1
        )
    )
    assert len(result.hits) == 1
    assert query.casefold() in result.hits[0].summary.casefold()
    assert result.hits[0].truncated
    detail = asyncio.run(
        port.read_entity(program_id=uuid4(), version_id=uuid4(), entity_id=identity, query=query)
    )
    assert query.casefold() in detail.matched_context.casefold()
    assert detail.truncated


@pytest.mark.parametrize("key_flag", [False, ""])
def test_api_rejects_incomplete_credentials_before_insert(monkeypatch, key_flag):
    settings = Settings(
        _env_file=None,
        postgres_password="test",
        chat_enabled=True,
        chat_model="test",
        chat_base_url="https://example.invalid/v1",
        chat_api_key="",
        chat_credentials_configured=key_flag,
    )
    monkeypatch.setattr(api, "get_settings", lambda: settings)

    class Repository:
        def create_turn(self, *_):
            raise AssertionError("Нельзя сохранять ход без конфигурации")

    with client_for(monkeypatch, Repository()) as client:
        response = client.post(
            f"/api/v1/programs/{uuid4()}/chat/sessions/{uuid4()}/messages",
            json={"text": "Вопрос"},
            headers={"Idempotency-Key": "key"},
        )
    assert response.status_code == 503


@pytest.mark.parametrize(
    "enabled,key,expected",
    [("false", "test-only", "False"), ("true", "test-only", "True"), ("true", "", None)],
)
def test_worker_config_is_explicit_and_consistent(enabled, key, expected):
    env = {
        **os.environ,
        "POSTGRES_PASSWORD": "test",
        "CHAT_ENABLED": enabled,
        "CHAT_MODEL": "test",
        "CHAT_BASE_URL": "https://example.invalid/v1",
        "CHAT_API_KEY": key,
    }
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "from app.chat_model import chat_agent; print(chat_agent is not None)",
        ],
        env=env,
        capture_output=True,
        text=True,
        timeout=15,
    )
    if expected is None:
        assert result.returncode != 0
        assert "CHAT_API_KEY" in result.stderr
        assert key == "" or key not in result.stderr
    else:
        assert result.returncode == 0, result.stderr
        assert result.stdout.strip() == expected
