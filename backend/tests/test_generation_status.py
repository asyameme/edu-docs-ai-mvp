"""Техническое выполнение читает Temporal, сохранённый ZIP не зависит от его истории."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

from temporalio.client import WorkflowExecutionStatus

from app.generation_api import generation_status


def test_active_generation_uses_temporal_without_database_execution_flags():
    jobs = SimpleNamespace(
        job=Mock(
            return_value={
                "status": "pending",
                "_namespace": "test",
                "_workflow_id": "generate-test",
            }
        )
    )
    handle = SimpleNamespace(
        describe=AsyncMock(return_value=SimpleNamespace(status=WorkflowExecutionStatus.RUNNING))
    )
    temporal = SimpleNamespace(namespace="test", get_workflow_handle=Mock(return_value=handle))
    result = asyncio.run(generation_status(jobs, temporal, "program", "job"))
    assert result["status"] == "processing"
    handle.describe.assert_awaited_once()


def test_saved_zip_result_does_not_require_temporal_history():
    jobs = SimpleNamespace(job=Mock(return_value={"status": "completed"}))
    temporal = Mock()
    result = asyncio.run(generation_status(jobs, temporal, "program", "job"))
    assert result["status"] == "completed"
    temporal.get_workflow_handle.assert_not_called()
