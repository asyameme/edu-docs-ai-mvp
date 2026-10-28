"""Текущий прогресс берётся из Temporal, долговечный итог — из бизнес-отчёта."""

import asyncio
from datetime import timedelta

from temporalio.client import WorkflowExecutionStatus, WorkflowQueryFailedError
from temporalio.service import RPCError, RPCStatusCode

from app.errors import DomainError

RPC_TIMEOUT = timedelta(seconds=3)


def interrupted(job):
    job.update(status="failed")
    job["issues"] = [
        *job["issues"],
        {
            "category": "workflow_interrupted",
            "severity": "error",
            "message": "Процесс остановлен в Temporal; доступен повтор обработки",
        },
    ]
    for item in job["files"]:
        if item["status"] == "pending":
            item.update(
                status="failed",
                error_code="workflow_interrupted",
                error_message="Обработка прервана до сохранения результата",
            )
    return job


async def read_status(repo, client, program_id, job_id):
    job = await asyncio.to_thread(repo.read, program_id, job_id)
    if client.namespace != job["_namespace"]:
        if job["_terminal"]:
            return job
        raise DomainError(503, "Пространство имён задачи недоступно в текущей конфигурации")
    handle = client.get_workflow_handle(job["_workflow_id"])
    try:
        description = await handle.describe(rpc_timeout=RPC_TIMEOUT)
        if description.status not in {
            WorkflowExecutionStatus.RUNNING,
            WorkflowExecutionStatus.COMPLETED,
        }:
            return job if job["_terminal"] else interrupted(job)
        if description.status == WorkflowExecutionStatus.COMPLETED and job["_terminal"]:
            return job
        if job["_legacy"]:
            # Старые истории V1 не переписываются: считаем завершённые этапы по их событиям.
            history = await asyncio.wait_for(handle.fetch_history(), timeout=3)
            scheduled, done = {}, 0
            for event in history.events:
                if event.HasField("activity_task_scheduled_event_attributes"):
                    scheduled[event.event_id] = (
                        event.activity_task_scheduled_event_attributes.activity_type.name
                    )
                if event.HasField("activity_task_completed_event_attributes"):
                    identity = event.activity_task_completed_event_attributes.scheduled_event_id
                    done += scheduled.get(identity) in {"import_parse_file", "import_file_failed"}
            job.update(status="processing", progress=5 + 90 * done // max(len(job["files"]), 1))
            if description.status == WorkflowExecutionStatus.COMPLETED:
                final = await asyncio.to_thread(repo.read, program_id, job_id)
                return final if final["_terminal"] else interrupted(final)
        else:
            state = await handle.query("import_progress", result_type=dict, rpc_timeout=RPC_TIMEOUT)
            if state.get("status") in {"completed", "partial", "failed"}:
                fresh = await asyncio.to_thread(repo.read, program_id, job_id)
                if fresh["_workflow_id"] != job["_workflow_id"]:
                    return fresh
                job = fresh
            for key in ("status", "progress", "attempt", "updated_at"):
                if key in state:
                    job[key] = state[key]
            states = {item["document_id"]: item["status"] for item in state.get("files", [])}
            for item in job["files"]:
                if item["document_id"] in states:
                    item["status"] = states[item["document_id"]]
        return job
    except RPCError as error:
        if error.status == RPCStatusCode.NOT_FOUND or job["_terminal"]:
            # До первого запуска это очередь запросов; после retention — сохранённый итог.
            return job
        raise DomainError(503, "Текущий прогресс временно недоступен в Temporal") from error
    except (WorkflowQueryFailedError, TimeoutError) as error:
        if job["_terminal"]:
            return job
        raise DomainError(503, "Текущий прогресс временно недоступен в Temporal") from error
