from typing import Annotated
from uuid import UUID

import psycopg
from fastapi import APIRouter, Depends, Header, Query, Request, UploadFile
from fastapi.responses import FileResponse

from app.config import get_settings
from app.domain import (
    DocumentRead,
    ProgramCreate,
    ProgramRead,
    VersionCreate,
    VersionRead,
    VersionRestore,
)
from app.repository import Repository
from app.storage import OriginalStorage

router = APIRouter(prefix="/api/v1/programs", tags=["Programs"])


def repository(request: Request):
    return Repository(request.app.state.resources.postgres)


def storage():
    settings = get_settings()
    return OriginalStorage(settings.original_files_path, settings.max_upload_bytes)


RepoDep = Annotated[Repository, Depends(repository)]
StorageDep = Annotated[OriginalStorage, Depends(storage)]
OperationKey = Annotated[
    str,
    Header(alias="Idempotency-Key", min_length=1, max_length=200, pattern=r"^[A-Za-z0-9._:-]+$"),
]
Limit = Annotated[int, Query(ge=1, le=100)]
Offset = Annotated[int, Query(ge=0)]


@router.post("", status_code=201)
def create_program(body: ProgramCreate, repo: RepoDep) -> ProgramRead:
    return repo.create_program(body.name)


@router.get("")
def programs(repo: RepoDep, limit: Limit = 50, offset: Offset = 0) -> list[ProgramRead]:
    return repo.programs(limit, offset)


@router.get("/{program_id}")
def program(program_id: UUID, repo: RepoDep) -> ProgramRead:
    return repo.program(program_id)


@router.post("/{program_id}/versions", status_code=201)
def save_version(
    program_id: UUID, body: VersionCreate, repo: RepoDep, operation_key: OperationKey
) -> VersionRead:
    return repo.save(program_id, body.expected_version_id, operation_key, snapshot=body.snapshot)


@router.get("/{program_id}/versions")
def versions(
    program_id: UUID, repo: RepoDep, limit: Limit = 50, offset: Offset = 0
) -> list[VersionRead]:
    return repo.versions(program_id, limit, offset)


@router.get("/{program_id}/versions/{version_id}")
def version(program_id: UUID, version_id: UUID, repo: RepoDep) -> VersionRead:
    return repo.version(program_id, version_id)


@router.post("/{program_id}/versions/{version_id}/restore", status_code=201)
def restore(
    program_id: UUID,
    version_id: UUID,
    body: VersionRestore,
    repo: RepoDep,
    operation_key: OperationKey,
) -> VersionRead:
    return repo.save(program_id, body.expected_version_id, operation_key, restore_id=version_id)


@router.post("/{program_id}/documents", status_code=201)
def upload_document(
    program_id: UUID, file: UploadFile, repo: RepoDep, originals: StorageDep
) -> DocumentRead:
    repo.program(program_id)
    stored = originals.save(file)
    try:
        return repo.add_document(program_id, stored)
    except (psycopg.OperationalError, psycopg.InterfaceError):
        # Подтверждение транзакции может потеряться после её фиксации сервером.
        # Сохраняем оригинал; проверка без удаления выявляет файлы без записи в БД.
        raise
    except Exception:
        originals.remove(stored["storage_key"])
        raise
    finally:
        file.file.close()


@router.get("/{program_id}/documents")
def documents(
    program_id: UUID, repo: RepoDep, limit: Limit = 50, offset: Offset = 0
) -> list[DocumentRead]:
    return repo.documents(program_id, limit, offset)


@router.get(
    "/{program_id}/documents/{document_id}/download",
    response_class=FileResponse,
    responses={200: {"content": {"application/octet-stream": {
        "schema": {"type": "string", "format": "binary"}
    }}}},
)
def download(
    program_id: UUID, document_id: UUID, repo: RepoDep, originals: StorageDep
) -> FileResponse:
    document = repo.document(program_id, document_id)
    return FileResponse(
        originals.download_path(document["storage_key"]),
        media_type=document["media_type"],
        filename=document["original_name"],
    )
