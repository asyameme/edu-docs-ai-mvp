"""Короткий процесс анализа без ожидания человека; текущие шаги принадлежат Temporal."""

from datetime import timedelta

from temporalio import workflow
from temporalio.common import RetryPolicy
from temporalio.exceptions import ActivityError


@workflow.defn(name="ReviewWorkflowV1")
class ReviewWorkflow:
    def __init__(self):
        self.state = {"status": "pending", "progress": 0}

    @workflow.query(name="review_progress")
    def progress(self) -> dict:
        return self.state

    @workflow.run
    async def run(self, request: dict) -> str:
        self.state = {"status": "processing", "progress": 5}
        try:
            outcome = await workflow.execute_activity(
                "review_execute_v1",
                request,
                result_type=str,
                start_to_close_timeout=timedelta(seconds=420),
                retry_policy=RetryPolicy(maximum_attempts=3),
            )
        except ActivityError:
            await workflow.execute_activity(
                "review_failed_v1",
                request,
                start_to_close_timeout=timedelta(seconds=30),
                retry_policy=RetryPolicy(maximum_interval=timedelta(seconds=30)),
            )
            outcome = "failed"
        self.state = {"status": outcome, "progress": 100}
        return outcome
