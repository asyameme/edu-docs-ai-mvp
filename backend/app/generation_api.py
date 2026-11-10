"""Запуск генерации, просмотр состояния и скачивание готового ZIP."""

from datetime import datetime
from typing import Literal
from uuid import UUID

from fastapi import APIRouter
from fastapi.responses import Response
from pydantic import BaseModel, model_validator

from app.generation_repository import GenerationRepository
from app.program_api import OperationKey, RepoDep

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
    status: Literal["pending", "processing", "completed", "failed"]
    error: str | None
    size_bytes: int | None
    sha256: str | None
    created_at: datetime


@router.post("", status_code=202)
def create_generation(
    program_id: UUID, body: GenerationCreate, operation_key: OperationKey, repo: RepoDep
) -> GenerationRead:
    jobs = GenerationRepository(repo.pool)
    job_id = jobs.create(program_id, body.version_id, body.import_job_id, operation_key)
    return jobs.job(program_id, job_id)


@router.get("/{job_id}")
def read_generation(program_id: UUID, job_id: UUID, repo: RepoDep) -> GenerationRead:
    return GenerationRepository(repo.pool).job(program_id, job_id)


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
