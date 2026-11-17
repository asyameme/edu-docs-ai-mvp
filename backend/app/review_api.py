"""Просмотр анализа, выбор вариантов и подтверждение конкретной версии программы."""

import asyncio
from datetime import timedelta
from typing import Annotated, Literal
from uuid import UUID

import psycopg
from fastapi import APIRouter, Depends, Request
from fastapi.responses import FileResponse
from fastapi.sse import EventSourceResponse, ServerSentEvent
from psycopg_pool import PoolTimeout
from pydantic import BaseModel
from temporalio.client import WorkflowExecutionStatus, WorkflowQueryFailedError
from temporalio.service import RPCError, RPCStatusCode

from app.config import get_settings
from app.domain import VersionRead
from app.errors import DomainError
from app.program_api import Limit, Offset, OperationKey, RepoDep
from app.repository import Repository
from app.review_contract import CheckCreate, ConfirmCreate, DecisionCreate, ReviewCreate
from app.review_logic import digest
from app.review_repository import ReviewRepository

router = APIRouter(prefix="/api/v1/programs/{program_id}", tags=["Reviews"])


class ReviewJobRead(BaseModel):
    id: UUID
    review_id: UUID
    purpose: Literal["prepare", "check"]
    input_version_id: UUID
    output_version_id: UUID | None
    status: Literal["pending", "processing", "completed", "failed", "conflict"]
    progress: int
    message: str | None = None
    analysis_version_id: UUID | None = None
    current_review_version_id: UUID | None = None


async def job_state(repo, client, program_id, job_id):
    row = await asyncio.to_thread(repo.job, program_id, job_id)
    result = {
        "id": row["id"],
        "review_id": row["parameters"]["review_id"],
        "purpose": row["parameters"]["purpose"],
        "input_version_id": row["input_version_id"],
        "output_version_id": row["output_version_id"],
        "status": row["report"].get("outcome", "pending"),
        "progress": 100 if "outcome" in row["report"] else 0,
        "message": row["report"].get("message"),
    }
    if "outcome" not in row["report"]:
        if client.namespace != row["temporal_namespace"]:
            raise DomainError(503, "Пространство имён анализа недоступно")
        handle = client.get_workflow_handle(row["workflow_id"])
        try:
            description = await handle.describe(rpc_timeout=timedelta(seconds=3))
            if description.status not in {
                WorkflowExecutionStatus.RUNNING,
                WorkflowExecutionStatus.COMPLETED,
            }:
                result.update(
                    status="failed", message="Процесс прерван в Temporal; создайте новый запрос"
                )
            else:
                state = await handle.query(
                    "review_progress", result_type=dict, rpc_timeout=timedelta(seconds=3)
                )
                if state["status"] in {"completed", "failed", "conflict"}:
                    fresh = await asyncio.to_thread(repo.job, program_id, job_id)
                    result.update(
                        status=fresh["report"].get("outcome", "processing"),
                        output_version_id=fresh["output_version_id"],
                        message=fresh["report"].get("message"),
                    )
                else:
                    result["status"] = state["status"]
                result["progress"] = state["progress"]
        except RPCError as error:
            if error.status != RPCStatusCode.NOT_FOUND:
                raise DomainError(503, "Текущий прогресс анализа временно недоступен") from error
        except (WorkflowQueryFailedError, TimeoutError) as error:
            raise DomainError(503, "Текущий прогресс анализа временно недоступен") from error
    if result["purpose"] == "prepare":
        result["analysis_version_id"] = result["output_version_id"]
        current = await asyncio.to_thread(Repository(repo.pool).program, program_id)
        version = await asyncio.to_thread(
            Repository(repo.pool).version, program_id, current["current_version_id"]
        )
        if (version["snapshot"].get("review") or {}).get("review_id") == str(job_id):
            result["current_review_version_id"] = current["current_version_id"]
    return result


@router.post("/reviews", status_code=202)
def create_review(
    program_id: UUID, body: ReviewCreate, operation_key: OperationKey, repo: RepoDep
) -> ReviewJobRead:
    reviews = ReviewRepository(repo.pool)
    job_id = reviews.create(program_id, operation_key, body)
    row = reviews.job(program_id, job_id)
    return ReviewJobRead(
        id=job_id,
        review_id=job_id,
        purpose="prepare",
        input_version_id=row["input_version_id"],
        output_version_id=row["output_version_id"],
        status=row["report"].get("outcome", "pending"),
        progress=100 if "outcome" in row["report"] else 0,
    )


@router.get("/reviews")
async def list_reviews(
    program_id: UUID, repo: RepoDep, request: Request, limit: Limit = 50, offset: Offset = 0
) -> list[ReviewJobRead]:
    reviews = ReviewRepository(repo.pool)
    ids = await asyncio.to_thread(reviews.listing, program_id, limit, offset)
    return [
        await job_state(reviews, request.app.state.resources.temporal, program_id, identity)
        for identity in ids
    ]


@router.get("/reviews/{review_id}")
async def read_review(
    program_id: UUID, review_id: UUID, repo: RepoDep, request: Request
) -> ReviewJobRead:
    reviews = ReviewRepository(repo.pool)
    row = await asyncio.to_thread(reviews.job, program_id, review_id)
    if row["parameters"]["purpose"] != "prepare":
        raise DomainError(404, "Подготовка не найдена")
    return await job_state(reviews, request.app.state.resources.temporal, program_id, review_id)


@router.get("/reviews/{review_id}/events", response_class=EventSourceResponse)
async def review_events(
    program_id: UUID,
    review_id: UUID,
    repo: RepoDep,
    request: Request,
    initial: Annotated[dict, Depends(read_review)],
):
    previous = None
    state = ReviewJobRead.model_validate(initial).model_dump(mode="json")
    while not await request.is_disconnected():
        identity = digest(state)
        if identity != previous:
            yield ServerSentEvent(data=state, event="status", id=identity, retry=3000)
            previous = identity
        if state["status"] in {"completed", "failed", "conflict"}:
            return
        await asyncio.sleep(1)
        try:
            state = ReviewJobRead.model_validate(
                await read_review(program_id, review_id, repo, request)
            ).model_dump(mode="json")
        except DomainError as error:
            if error.status != 503:
                raise
            yield ServerSentEvent(data={"message": error.detail}, event="unavailable")
            return
        except (psycopg.Error, PoolTimeout, OSError):
            yield ServerSentEvent(
                data={"message": "Хранилище временно недоступно"}, event="unavailable"
            )
            return


@router.get("/versions/{version_id}/review")
def read_version_review(
    program_id: UUID, version_id: UUID, repo: RepoDep, limit: Limit = 50, offset: Offset = 0
) -> dict:
    return ReviewRepository(repo.pool).details(program_id, version_id, limit, offset)


@router.post("/reviews/{review_id}/decisions", status_code=201)
def decide(
    program_id: UUID,
    review_id: UUID,
    body: DecisionCreate,
    operation_key: OperationKey,
    repo: RepoDep,
) -> VersionRead:
    return ReviewRepository(repo.pool).decide(program_id, review_id, operation_key, body)


@router.post("/reviews/{review_id}/checks", status_code=202)
def create_check(
    program_id: UUID, review_id: UUID, body: CheckCreate, operation_key: OperationKey, repo: RepoDep
) -> ReviewJobRead:
    reviews = ReviewRepository(repo.pool)
    job_id = reviews.create(program_id, operation_key, body, purpose="check", review_id=review_id)
    row = reviews.job(program_id, job_id)
    return ReviewJobRead(
        id=job_id,
        review_id=review_id,
        purpose="check",
        input_version_id=row["input_version_id"],
        output_version_id=None,
        status=row["report"].get("outcome", "pending"),
        progress=100 if "outcome" in row["report"] else 0,
    )


@router.get("/reviews/{review_id}/checks/{check_job_id}")
async def read_check(
    program_id: UUID, review_id: UUID, check_job_id: UUID, repo: RepoDep, request: Request
) -> dict:
    reviews = ReviewRepository(repo.pool)
    row = await asyncio.to_thread(reviews.job, program_id, check_job_id)
    if (
        row["parameters"].get("review_id") != str(review_id)
        or row["parameters"]["purpose"] != "check"
    ):
        raise DomainError(404, "Проверка не найдена")
    state = await job_state(reviews, request.app.state.resources.temporal, program_id, check_job_id)
    row = await asyncio.to_thread(reviews.job, program_id, check_job_id)
    return {
        **ReviewJobRead.model_validate(state).model_dump(mode="json"),
        "content_hash": row["parameters"]["content_hash"],
        "report": row["report"].get("result"),
    }


@router.post("/reviews/{review_id}/confirm", status_code=201)
def confirm(
    program_id: UUID,
    review_id: UUID,
    body: ConfirmCreate,
    operation_key: OperationKey,
    repo: RepoDep,
) -> VersionRead:
    return ReviewRepository(repo.pool).confirm(program_id, review_id, operation_key, body)


@router.get(
    "/documents/{document_id}/artifacts/{artifact_id}/download", response_class=FileResponse
)
def download_artifact(
    program_id: UUID, document_id: UUID, artifact_id: UUID, repo: RepoDep
) -> FileResponse:
    with repo.pool.connection() as connection:
        row = connection.execute(
            "SELECT storage_key FROM document_artifacts WHERE program_id=%s "
            "AND document_id=%s AND id=%s AND kind='markdown'",
            (program_id, document_id, artifact_id),
        ).fetchone()
    if row is None:
        raise DomainError(404, "Артефакт не найден")
    path = get_settings().artifact_files_path / str(UUID(row[0]))
    if not path.is_file():
        raise DomainError(503, "Markdown временно недоступен")
    return FileResponse(path, media_type="text/markdown; charset=utf-8", filename="content.md")
