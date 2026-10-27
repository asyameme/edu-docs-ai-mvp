"""Загрузка пакета, чтение состояния и просмотр результата без подтверждения новой версии."""

import asyncio
import hashlib
import io
import json
from collections.abc import AsyncIterable
from datetime import datetime
from typing import Annotated, Literal
from uuid import UUID

import psycopg
from fastapi import APIRouter, Depends, Request, UploadFile
from fastapi.sse import EventSourceResponse, ServerSentEvent
from psycopg_pool import PoolTimeout
from pydantic import BaseModel
from starlette.datastructures import UploadFile as StoredUpload

from app.domain import Snapshot
from app.errors import DomainError
from app.import_archive import read_package
from app.import_repository import ImportRepository
from app.program_api import OperationKey, RepoDep, StorageDep

router = APIRouter(prefix="/api/v1/programs/{program_id}/imports", tags=["Imports"])


class ImportFileRead(BaseModel):
    document_id: UUID
    member_name: str
    sha256: str
    status: Literal["pending", "processing", "completed", "failed"]
    error_code: str | None
    error_message: str | None


class ImportRead(BaseModel):
    id: UUID
    program_id: UUID
    attempt: int
    status: Literal["pending", "processing", "completed", "partial", "failed"]
    progress: int
    issues: list[dict]
    files: list[ImportFileRead]
    created_at: datetime
    updated_at: datetime


@router.post("", status_code=202)
def upload_package(
    program_id: UUID,
    file: UploadFile,
    operation_key: OperationKey,
    repo: RepoDep,
    originals: StorageDep,
) -> ImportRead:
    repo.program(program_id)
    stored = []
    try:
        fingerprint, members = read_package(file, originals.max_bytes)
        for member in members:
            upload = StoredUpload(filename=member.name, file=io.BytesIO(member.content))
            stored.append((member.name, originals.save(upload)))
        imports = ImportRepository(repo.pool)
        job_id, used = imports.create(program_id, fingerprint, operation_key, stored)
    except (psycopg.OperationalError, psycopg.InterfaceError):
        # При потерянном подтверждении commit оригиналы сохраняем для штатного аудита.
        raise
    except Exception:
        for _, item in stored:
            originals.remove(item["storage_key"])
        raise
    finally:
        file.file.close()
    for _, item in stored:
        if item["id"] not in used:
            originals.remove(item["storage_key"])
    return imports.read(program_id, job_id)


@router.get("/{job_id}")
def read_import(program_id: UUID, job_id: UUID, repo: RepoDep) -> ImportRead:
    return ImportRepository(repo.pool).read(program_id, job_id)


@router.get("/{job_id}/events", response_class=EventSourceResponse)
async def import_events(
    program_id: UUID,
    job_id: UUID,
    request: Request,
    repo: RepoDep,
    initial: Annotated[dict, Depends(read_import)],
) -> AsyncIterable[ServerSentEvent]:
    state, previous = initial, None
    imports = ImportRepository(repo.pool)
    while not await request.is_disconnected():
        data = ImportRead.model_validate(state).model_dump(mode="json")
        identity = hashlib.sha256(json.dumps(data, sort_keys=True).encode()).hexdigest()
        if identity != previous:
            yield ServerSentEvent(data=data, event="status", id=identity, retry=3000)
            previous = identity
        if state["status"] in {"completed", "partial", "failed"}:
            return
        await asyncio.sleep(1)
        try:
            state = await asyncio.to_thread(imports.read, program_id, job_id)
        except (psycopg.Error, PoolTimeout, OSError):
            yield ServerSentEvent(
                data={"message": "Состояние временно недоступно; соединение будет восстановлено"},
                event="unavailable",
            )
            return


@router.post("/{job_id}/retry", status_code=202)
def retry_import(program_id: UUID, job_id: UUID, repo: RepoDep) -> ImportRead:
    imports = ImportRepository(repo.pool)
    imports.retry(program_id, job_id)
    return imports.read(program_id, job_id)


@router.get("/{job_id}/result")
def read_result(program_id: UUID, job_id: UUID, repo: RepoDep) -> Snapshot:
    job = ImportRepository(repo.pool).read(program_id, job_id, include_output=True)
    if job["snapshot"] is None:
        raise DomainError(409, "Результат разбора ещё не готов или обработка завершилась ошибкой")
    return Snapshot.model_validate(job["snapshot"])
