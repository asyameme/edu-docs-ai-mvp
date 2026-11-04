"""Настоящее выполнение и повторы Temporal в отдельной тестовой очереди."""

import asyncio
import os
from datetime import timedelta
from uuid import uuid4

import pytest
from temporalio import activity
from temporalio.client import Client, WorkflowFailureError
from temporalio.exceptions import ApplicationError
from temporalio.worker import Replayer, Worker

from app.config import get_settings
from app.workflows import DiagnosticInput, DiagnosticWorkflow

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(os.getenv("RUN_INTEGRATION") != "1", reason="Needs isolated Temporal"),
]


class FaultActivity:
    def __init__(self, mode):
        self.mode = mode
        self.attempts = []
        self.failures_remaining = 2

    @activity.defn(name="diagnostic_echo")
    async def run(self, marker: str) -> dict[str, str]:
        attempt = activity.info().attempt
        self.attempts.append(attempt)
        if self.mode == "permanent":
            raise ApplicationError("Permanent format error", non_retryable=True)
        if self.mode == "always":
            raise ApplicationError("Temporary dependency error")
        if self.mode == "recover" and self.failures_remaining:
            self.failures_remaining -= 1
            raise ApplicationError("Temporary dependency error")
        return {"marker": marker, "worker": "test-worker", "session": "test-session"}


@pytest.mark.parametrize(
    "mode,attempts",
    [
        ("recover", [1, 2, 3, 1]),
        ("always", [1, 2, 3]),
        ("permanent", [1]),
    ],
)
def test_activity_retry_behavior_through_temporal(mode, attempts):
    async def run():
        settings = get_settings()
        client = await Client.connect(
            settings.temporal_address, namespace=settings.temporal_namespace
        )
        queue = "acceptance-" + uuid4().hex
        probe = FaultActivity(mode)
        async with Worker(
            client, task_queue=queue, workflows=[DiagnosticWorkflow], activities=[probe.run]
        ):
            handle = await client.start_workflow(
                DiagnosticWorkflow.run,
                DiagnosticInput("test-marker"),
                id="retry-" + uuid4().hex,
                task_queue=queue,
                execution_timeout=timedelta(seconds=25),
            )
            if mode == "recover":
                result = await asyncio.wait_for(handle.result(), 30)
                assert result["marker"] == "test-marker"
                await Replayer(workflows=[DiagnosticWorkflow]).replay_workflow(
                    await handle.fetch_history()
                )
            else:
                with pytest.raises(WorkflowFailureError):
                    await asyncio.wait_for(handle.result(), 30)
            assert probe.attempts == attempts

    asyncio.run(run())


def test_concurrent_workflows_keep_their_own_markers():
    async def run():
        settings = get_settings()
        client = await Client.connect(
            settings.temporal_address, namespace=settings.temporal_namespace
        )

        async def one(marker):
            handle = await client.start_workflow(
                DiagnosticWorkflow.run,
                DiagnosticInput(marker),
                id="parallel-" + marker,
                task_queue=settings.temporal_task_queue,
                execution_timeout=timedelta(seconds=30),
            )
            result = await asyncio.wait_for(handle.result(), 35)
            assert (
                result["marker"]
                == result["before"]["marker"]
                == result["after"]["marker"]
                == marker
            )
            return result["workflow_id"]

        markers = [uuid4().hex for _ in range(8)]
        results = await asyncio.gather(*(one(marker) for marker in markers))
        assert len(set(results)) == len(markers)

    asyncio.run(run())
