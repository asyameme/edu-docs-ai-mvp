"""Долговечная фоновая задача сборки ZIP."""

from datetime import timedelta

from temporalio import workflow
from temporalio.common import RetryPolicy
from temporalio.exceptions import ActivityError


@workflow.defn(name="GenerationWorkflowV1")
class GenerationWorkflow:
    @workflow.run
    async def run(self, request: dict) -> str:
        try:
            return await workflow.execute_activity(
                "generation_build_v1",
                request,
                result_type=str,
                start_to_close_timeout=timedelta(minutes=15),
                retry_policy=RetryPolicy(maximum_attempts=3),
            )
        except ActivityError:
            await workflow.execute_activity(
                "generation_failed_v1",
                request,
                start_to_close_timeout=timedelta(seconds=30),
                retry_policy=RetryPolicy(maximum_interval=timedelta(seconds=30)),
            )
            return "failed"
