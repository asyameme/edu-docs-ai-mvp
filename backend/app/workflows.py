from dataclasses import dataclass
from datetime import timedelta

from temporalio import workflow
from temporalio.common import RetryPolicy
from temporalio.exceptions import ApplicationError

with workflow.unsafe.imports_passed_through():
    from app.activities import diagnostic_echo


@dataclass
class DiagnosticInput:
    marker: str
    delay_seconds: int = 0


@workflow.defn(name="DiagnosticWorkflowV1")
class DiagnosticWorkflow:
    @workflow.run
    async def run(self, request: DiagnosticInput) -> dict:
        if not 0 <= request.delay_seconds <= 300:
            raise ApplicationError("Invalid diagnostic delay", non_retryable=True)
        options = {
            "start_to_close_timeout": timedelta(seconds=10),
            "retry_policy": RetryPolicy(maximum_attempts=3),
        }
        before = await workflow.execute_activity(diagnostic_echo, request.marker, **options)
        if request.delay_seconds:
            await workflow.sleep(request.delay_seconds)
        after = await workflow.execute_activity(diagnostic_echo, request.marker, **options)
        return {
            "marker": request.marker,
            "workflow_id": workflow.info().workflow_id,
            "before": before,
            "after": after,
        }
