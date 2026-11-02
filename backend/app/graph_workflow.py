"""Устойчивое построение одной неизменяемой версии графа."""

from datetime import timedelta

from temporalio import workflow
from temporalio.common import RetryPolicy
from temporalio.exceptions import ActivityError


@workflow.defn(name="GraphWorkflowV1")
class GraphWorkflow:
    def __init__(self):
        self.state = {"status": "pending", "progress": 0}

    @workflow.query(name="graph_progress")
    def progress(self) -> dict:
        return self.state

    @workflow.run
    async def run(self, request: dict) -> str:
        self.state = {"status": "processing", "progress": 10}
        try:
            outcome = await workflow.execute_activity(
                "graph_build_v1",
                request,
                result_type=str,
                start_to_close_timeout=timedelta(seconds=120),
                retry_policy=RetryPolicy(maximum_attempts=3),
            )
        except ActivityError:
            await workflow.execute_activity(
                "graph_failed_v1",
                request,
                start_to_close_timeout=timedelta(seconds=30),
                retry_policy=RetryPolicy(maximum_interval=timedelta(seconds=30)),
            )
            outcome = "failed"
        self.state = {"status": outcome, "progress": 100}
        return outcome
