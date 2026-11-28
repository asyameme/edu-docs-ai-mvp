"""Загрузка пакета, чтение состояния и просмотр результата без подтверждения новой версии."""

import asyncio
import hashlib
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

from app.errors import DomainError
from app.import_archive import read_package
from app.import_repository import ImportRepository
from app.import_status import read_status
from app.parser_document import ParserDocument
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
    automatic_review_id: UUID | None = None


class ParsedDocumentRead(BaseModel):
    document_id: UUID
    member_name: str
    sha256: str
    parsed: ParserDocument


class ImportResultRead(BaseModel):
    job_id: UUID
    documents: list[ParsedDocumentRead]


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
            stored.append(
                (
                    member.name,
                    originals.save_bytes(
                        member.name, member.content, precomputed_sha256=member.sha256
                    ),
                    member.parse,
                )
            )
        imports = ImportRepository(repo.pool)
        job_id, _ = imports.create(program_id, fingerprint, operation_key, stored)
    finally:
        file.file.close()
    return imports.read(program_id, job_id)


@router.get("/{job_id}")
async def read_import(
    program_id: UUID, job_id: UUID, repo: RepoDep, request: Request
) -> ImportRead:
    return await read_status(
        ImportRepository(repo.pool), request.app.state.resources.temporal, program_id, job_id
    )


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
            state = await read_status(
                imports, request.app.state.resources.temporal, program_id, job_id
            )
        except (psycopg.Error, PoolTimeout, OSError):
            yield ServerSentEvent(
                data={"message": "Состояние временно недоступно; соединение будет восстановлено"},
                event="unavailable",
            )
            return
        except DomainError as error:
            if error.status != 503:
                raise
            yield ServerSentEvent(data={"message": error.detail}, event="unavailable")
            return


@router.post("/{job_id}/retry", status_code=202)
async def retry_import(
    program_id: UUID,
    job_id: UUID,
    repo: RepoDep,
    request: Request,
) -> ImportRead:
    imports = ImportRepository(repo.pool)
    state = await read_status(imports, request.app.state.resources.temporal, program_id, job_id)
    if state["status"] in {"partial", "failed"}:
        await asyncio.to_thread(imports.retry, program_id, job_id, state["_workflow_id"])
    return await asyncio.to_thread(imports.read, program_id, job_id)


@router.get("/{job_id}/result")
def read_result(program_id: UUID, job_id: UUID, repo: RepoDep) -> ImportResultRead:
    job = ImportRepository(repo.pool).read(program_id, job_id, include_output=True)
    documents = [
        {
            "document_id": item["document_id"],
            "member_name": item["member_name"],
            "sha256": item["sha256"],
            "parsed": item["output"],
        }
        for item in job["files"]
        if item.get("output") is not None and not item.get("error_code")
    ]
    if not documents or job["status"] not in {"completed", "partial"}:
        raise DomainError(409, "Результат разбора ещё не готов или обработка завершилась ошибкой")
    return ImportResultRead(job_id=job["id"], documents=documents)
