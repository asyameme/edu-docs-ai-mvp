"""Прогресс приходит из Temporal; недоступный процесс не подменяется SQL-статусом."""

import asyncio
from copy import deepcopy
from types import SimpleNamespace

import pytest
from temporalio.client import WorkflowExecutionStatus

from app.errors import DomainError
from app.import_status import read_status


def stored(terminal=False):
    return {
        "status": "completed" if terminal else "pending",
        "progress": 100 if terminal else 0,
        "attempt": 1,
        "issues": [],
        "_namespace": "education",
        "_workflow_id": "import-example-1",
        "_terminal": terminal,
        "_legacy": False,
        "files": [{"document_id": "document", "status": "pending"}],
    }


def test_active_progress_is_read_from_workflow_query():
    calls = []

    async def describe(**kwargs):
        return SimpleNamespace(status=WorkflowExecutionStatus.RUNNING)

    async def query(name, **kwargs):
        calls.append(name)
        return {
            "status": "processing",
            "progress": 42,
            "attempt": 1,
            "files": [{"document_id": "document", "status": "processing"}],
        }

    repo = SimpleNamespace(read=lambda *args: deepcopy(stored()))
    client = SimpleNamespace(
        namespace="education",
        get_workflow_handle=lambda identity: SimpleNamespace(describe=describe, query=query),
    )
    result = asyncio.run(read_status(repo, client, "program", "job"))
    assert calls == ["import_progress"]
    assert result["status"] == "processing"
    assert result["progress"] == 42
    assert result["files"][0]["status"] == "processing"


def test_unavailable_temporal_does_not_fabricate_current_progress():
    async def describe(**kwargs):
        raise TimeoutError("Temporal unavailable")

    repo = SimpleNamespace(read=lambda *args: deepcopy(stored()))
    client = SimpleNamespace(
        namespace="education",
        get_workflow_handle=lambda identity: SimpleNamespace(describe=describe),
    )
    with pytest.raises(DomainError) as error:
        asyncio.run(read_status(repo, client, "program", "job"))
    assert error.value.status == 503


def test_durable_final_report_remains_available_without_temporal():
    async def describe(**kwargs):
        raise TimeoutError("Temporal unavailable")

    repo = SimpleNamespace(read=lambda *args: deepcopy(stored(terminal=True)))
    client = SimpleNamespace(
        namespace="education",
        get_workflow_handle=lambda identity: SimpleNamespace(describe=describe),
    )
    result = asyncio.run(read_status(repo, client, "program", "job"))
    assert result["status"] == "completed"
    assert result["progress"] == 100
