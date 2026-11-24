"""Durable background construction of the graph after the final review choice."""

from datetime import timedelta

from temporalio import workflow
from temporalio.common import RetryPolicy
from temporalio.exceptions import ActivityError


@workflow.defn(name="GraphBuildWorkflowV1")
class GraphBuildWorkflow:
    @workflow.run
    async def run(self, request: dict) -> str:
        try:
            return await workflow.execute_activity(
                "review_build_graph_v1",
                request,
                result_type=str,
                start_to_close_timeout=timedelta(minutes=15),
                retry_policy=RetryPolicy(maximum_attempts=3),
            )
        except ActivityError:
            await workflow.execute_activity(
                "review_graph_build_failed_v1",
                request,
                start_to_close_timeout=timedelta(seconds=30),
                retry_policy=RetryPolicy(maximum_attempts=3),
            )
            return "failed"
