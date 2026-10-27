from dataclasses import dataclass
from datetime import timedelta

from temporalio import workflow
from temporalio.common import RetryPolicy
from temporalio.exceptions import ActivityError, ApplicationError

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


@workflow.defn(name="ImportWorkflowV1")
class ImportWorkflow:
    @workflow.run
    async def run(self, request: dict) -> None:
        database_options = {
            "start_to_close_timeout": timedelta(seconds=30),
            "retry_policy": RetryPolicy(maximum_interval=timedelta(seconds=30)),
        }
        documents = await workflow.execute_activity(
            "import_begin",
            request,
            result_type=list[str],
            **database_options,
        )
        for document_id in documents:
            file_request = {**request, "document_id": document_id}
            try:
                await workflow.execute_activity(
                    "import_parse_file",
                    file_request,
                    start_to_close_timeout=timedelta(seconds=120),
                    retry_policy=RetryPolicy(maximum_attempts=3),
                )
            except ActivityError:
                await workflow.execute_activity(
                    "import_file_failed", file_request, **database_options
                )
        await workflow.execute_activity("import_finish", request, **database_options)
