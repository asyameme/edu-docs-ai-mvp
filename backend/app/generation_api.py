"""Запуск генерации, просмотр состояния и скачивание готового ZIP."""

import asyncio
from datetime import datetime, timedelta
from typing import Literal
from uuid import UUID

from fastapi import APIRouter, Request
from fastapi.responses import Response
from pydantic import BaseModel, Field, model_validator
from temporalio.client import WorkflowExecutionStatus
from temporalio.service import RPCError, RPCStatusCode

from app.errors import DomainError
from app.generation_repository import GenerationRepository
from app.program_api import Limit, Offset, OperationKey, RepoDep

router = APIRouter(prefix="/api/v1/programs/{program_id}/generation", tags=["Generation"])


class GenerationCreate(BaseModel):
    version_id: UUID | None = None
    import_job_id: UUID | None = None

    @model_validator(mode="after")
    def exactly_one_source(self):
        if (self.version_id is None) == (self.import_job_id is None):
            raise ValueError("Укажите либо version_id, либо import_job_id")
        return self


class GenerationRead(BaseModel):
    id: UUID
    program_id: UUID
    version_id: UUID | None
    import_job_id: UUID | None
    version_number: int | None = None
    status: Literal["pending", "processing", "completed", "failed"]
    error: str | None
    size_bytes: int | None
    sha256: str | None
    created_at: datetime
    # Что генерация изменила в документах и что оставила для ручной правки.
    applied: list[str] = Field(default_factory=list)
    notes: list[str] = Field(default_factory=list)


@router.get("")
def list_generations(
    program_id: UUID, repo: RepoDep, limit: Limit = 20, offset: Offset = 0
) -> list[GenerationRead]:
    return GenerationRepository(repo.pool).listing(program_id, limit, offset)


@router.post("", status_code=202)
def create_generation(
    program_id: UUID, body: GenerationCreate, operation_key: OperationKey, repo: RepoDep
) -> GenerationRead:
    jobs = GenerationRepository(repo.pool)
    job_id = jobs.create(program_id, body.version_id, body.import_job_id, operation_key)
    return jobs.job(program_id, job_id)


# Объявлен до /{job_id}: иначе путь «composition» попадёт в проверку UUID.
@router.get("/composition")
def read_composition(program_id: UUID, repo: RepoDep) -> dict:
    return GenerationRepository(repo.pool).composition(program_id)


@router.get("/{job_id}")
async def read_generation(
    program_id: UUID, job_id: UUID, repo: RepoDep, request: Request
) -> GenerationRead:
    return await generation_status(
        GenerationRepository(repo.pool), request.app.state.resources.temporal, program_id, job_id
    )


async def generation_status(jobs, temporal, program_id, job_id):
    result = await asyncio.to_thread(jobs.job, program_id, job_id)
    if result["status"] in {"completed", "failed"}:
        return result
    if temporal.namespace != result["_namespace"]:
        raise DomainError(503, "Пространство имён генерации недоступно")
    try:
        state = await temporal.get_workflow_handle(result["_workflow_id"]).describe(
            rpc_timeout=timedelta(seconds=3)
        )
    except RPCError as error:
        if error.status == RPCStatusCode.NOT_FOUND:
            return result
        raise DomainError(503, "Текущий статус генерации временно недоступен") from error
    if state.status == WorkflowExecutionStatus.RUNNING:
        result["status"] = "processing"
    else:
        result = await asyncio.to_thread(jobs.job, program_id, job_id)
        if result["status"] not in {"completed", "failed"}:
            result.update(
                status="failed", error="Процесс генерации прерван без сохранённого результата"
            )
    return result


@router.get("/{job_id}/download")
def download_generation(program_id: UUID, job_id: UUID, repo: RepoDep) -> Response:
    package = GenerationRepository(repo.pool).package(program_id, job_id)
    return Response(
        content=bytes(package["content"]),
        media_type="application/zip",
        headers={
            "Content-Disposition": 'attachment; filename="program.zip"',
            "X-Content-SHA256": package["sha256"],
        },
    )
