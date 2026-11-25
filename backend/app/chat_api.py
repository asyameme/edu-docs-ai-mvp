"""HTTP API сессий и ходов чата; прогресс отдаётся через SSE."""

import asyncio
import logging
from collections.abc import AsyncIterator
from datetime import datetime, timedelta
from typing import Annotated, Literal
from uuid import UUID

from fastapi import APIRouter, Depends, Header, Query, Request
from fastapi.sse import EventSourceResponse, ServerSentEvent
from pydantic import BaseModel, Field, TypeAdapter, field_validator
from temporalio.client import WorkflowExecutionStatus
from temporalio.service import RPCError, RPCStatusCode

from app.chat_repository import ChatRepository
from app.chat_write import ChatWriteRepository
from app.config import get_settings
from app.domain import Contract
from app.errors import DomainError
from app.program_api import OperationKey
from worker.agent.contracts import ChatOutput

router = APIRouter(prefix="/api/v1/programs/{program_id}/chat", tags=["Chat"])
TurnStatus = Literal["pending", "completed", "failed"]
ChatResult = Annotated[ChatOutput, Field(discriminator="kind")]
RESULT_ADAPTER = TypeAdapter(ChatResult)
MAX_TEMPORAL_NOT_FOUND_RETRIES = 15
logger = logging.getLogger(__name__)


class SessionCreate(Contract):
    version_id: UUID | None = None


class ProposalAccept(Contract):
    expected_version_id: UUID
    signature: str = Field(min_length=1, max_length=120)

    @field_validator("signature")
    @classmethod
    def signature_is_not_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("Нужна подпись пользователя")
        return value.strip()


@router.post("/sessions/{session_id}/runs/{turn_id}/accept", status_code=201)
def accept_proposal(
    program_id: UUID, session_id: UUID, turn_id: UUID, body: ProposalAccept,
    operation_key: OperationKey, request: Request,
) -> dict:
    return ChatWriteRepository(request.app.state.resources.postgres).accept(
        program_id, session_id, turn_id, body, operation_key
    )


class MessageCreate(Contract):
    text: str = Field(min_length=1, max_length=12000)

    @field_validator("text")
    @classmethod
    def normalize_text(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("Сообщение не должно быть пустым")
        return value


class SessionRead(BaseModel):
    id: UUID
    program_id: UUID
    version_id: UUID
    title: str
    created_at: datetime
    updated_at: datetime


class TurnRead(BaseModel):
    id: UUID
    status: TurnStatus
    run_id: UUID


class TurnHistoryRead(TurnRead):
    user_text: str
    result: ChatResult | None
    created_at: datetime
    completed_at: datetime | None


class SessionHistoryRead(SessionRead):
    turns: list[TurnHistoryRead]


class ChatStatusData(BaseModel):
    run_id: UUID
    status: Literal["pending", "processing"]


class ChatFailureData(BaseModel):
    run_id: UUID
    message: str = "Не удалось выполнить запрос. Попробуйте отправить его ещё раз."


class ChatUnavailableData(BaseModel):
    run_id: UUID
    message: str = (
        "Состояние запроса временно недоступно. "
        "Обновите страницу позже, чтобы получить результат."
    )


class ChatStatusEvent(BaseModel):
    id: UUID
    event: Literal["status"]
    data: ChatStatusData


class ChatCompletedEvent(BaseModel):
    id: UUID
    event: Literal["completed"]
    data: ChatResult


class ChatFailedEvent(BaseModel):
    id: UUID
    event: Literal["failed"]
    data: ChatFailureData


class ChatUnavailableEvent(BaseModel):
    id: UUID
    event: Literal["unavailable"]
    data: ChatUnavailableData


ChatEvent = Annotated[
    ChatStatusEvent | ChatCompletedEvent | ChatFailedEvent | ChatUnavailableEvent,
    Field(discriminator="event"),
]


class ChatError(BaseModel):
    detail: str


class ChatConfigRead(BaseModel):
    available: bool


def repo(request):
    return ChatRepository(request.app.state.resources.postgres)


@router.get("/config", response_model=ChatConfigRead)
def chat_config():
    """Возвращает фронтенду безопасный признак готовности API-модели чата."""
    return ChatConfigRead(available=get_settings().chat_api_ready)


def checked_run_session(program_id: UUID, run_id: UUID, request: Request) -> UUID:
    """Проверяет ход до запуска SSE producer, пока можно вернуть HTTP-ошибку."""
    row = repo(request).turn_input(run_id)
    if row["program_id"] != program_id:
        raise DomainError(404, "Ход чата не найден")
    return row["session_id"]


def completed_event(run_id: UUID, result) -> ServerSentEvent:
    return ServerSentEvent(
        id=str(run_id), event="completed", data=RESULT_ADAPTER.validate_python(result)
    )


def failed_event(run_id: UUID) -> ServerSentEvent:
    return ServerSentEvent(id=str(run_id), event="failed", data=ChatFailureData(run_id=run_id))


def unavailable_event(run_id: UUID) -> ServerSentEvent:
    return ServerSentEvent(
        id=str(run_id), event="unavailable", data=ChatUnavailableData(run_id=run_id)
    )


@router.post(
    "/sessions",
    status_code=201,
    response_model=SessionRead,
    responses={404: {"model": ChatError}},
)
def create_session(program_id: UUID, body: SessionCreate, request: Request):
    return repo(request).create_session(program_id, body.version_id)


@router.get("/sessions", response_model=list[SessionRead])
def list_sessions(
    program_id: UUID,
    request: Request,
    limit: Annotated[int, Query(ge=1, le=100)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
):
    return repo(request).sessions(program_id, limit, offset)


@router.get(
    "/sessions/{session_id}",
    response_model=SessionHistoryRead,
    responses={404: {"model": ChatError}},
)
def get_session(program_id: UUID, session_id: UUID, request: Request):
    return repo(request).session(program_id, session_id)


@router.post(
    "/sessions/{session_id}/messages",
    status_code=202,
    response_model=TurnRead,
    responses={404: {"model": ChatError}, 409: {"model": ChatError}, 503: {"model": ChatError}},
)
def send_message(
    program_id: UUID,
    session_id: UUID,
    body: MessageCreate,
    request: Request,
    idempotency_key: Annotated[
        str,
        Header(
            alias="Idempotency-Key", min_length=1, max_length=200, pattern=r"^[A-Za-z0-9._:-]+$"
        ),
    ],
):
    settings = get_settings()
    if not settings.chat_api_ready:
        raise DomainError(503, "Модель чата не настроена")
    repository = repo(request)
    identity = repository.create_turn(program_id, session_id, idempotency_key, body.text)
    state = repository.turn_status(identity)
    return {"id": identity, "status": state["status"], "run_id": identity}


@router.get(
    "/runs/{run_id}/events",
    response_class=EventSourceResponse,
    responses={
        200: {
            "model": ChatEvent,
            "description": (
                "SSE-поток. Схема описывает одно событие: id, event и JSON data. "
                "В потоке поля передаются строками id:/event:/data:, "
                "события разделены пустой строкой."
            ),
        },
        404: {"model": ChatError},
    },
)
async def run_events(
    program_id: UUID,
    run_id: UUID,
    request: Request,
    session_id: Annotated[UUID, Depends(checked_run_session)],
) -> AsyncIterator[ServerSentEvent]:
    repository = repo(request)
    temporal_not_found_retries = 0

    async def events():
        nonlocal temporal_not_found_retries
        while not await request.is_disconnected():
            state = await asyncio.to_thread(repository.session, program_id, session_id)
            turn = next(item for item in state["turns"] if item["id"] == run_id)
            if turn["status"] == "completed":
                yield completed_event(run_id, turn["result"])
                return
            if turn["status"] == "failed":
                yield failed_event(run_id)
                return
            try:
                description = await asyncio.wait_for(
                    request.app.state.resources.temporal.get_workflow_handle(
                        f"chat-{run_id}"
                    ).describe(rpc_timeout=timedelta(seconds=3)),
                    timeout=3,
                )
            except RPCError as error:
                if error.status == RPCStatusCode.NOT_FOUND:
                    temporal_not_found_retries += 1
                    if temporal_not_found_retries < MAX_TEMPORAL_NOT_FOUND_RETRIES:
                        yield ServerSentEvent(
                            id=str(run_id),
                            event="status",
                            data=ChatStatusData(run_id=run_id, status="pending"),
                        )
                        await asyncio.sleep(1)
                        continue
                logger.warning("Статус чата в Temporal недоступен: %s", error.status.name)
                yield unavailable_event(run_id)
                return
            except Exception as error:
                logger.warning("Ошибка чтения статуса чата в Temporal: %s", type(error).__name__)
                yield unavailable_event(run_id)
                return
            temporal_not_found_retries = 0
            if description.status == WorkflowExecutionStatus.RUNNING:
                status = "processing"
            elif description.status in {
                WorkflowExecutionStatus.COMPLETED,
                WorkflowExecutionStatus.FAILED,
                WorkflowExecutionStatus.CANCELED,
                WorkflowExecutionStatus.TERMINATED,
                WorkflowExecutionStatus.TIMED_OUT,
                WorkflowExecutionStatus.CONTINUED_AS_NEW,
            }:
                state = await asyncio.to_thread(repository.session, program_id, session_id)
                turn = next(item for item in state["turns"] if item["id"] == run_id)
                if turn["status"] == "completed":
                    yield completed_event(run_id, turn["result"])
                    return
                if turn["status"] == "failed":
                    yield failed_event(run_id)
                    return
                await asyncio.to_thread(repository.fail, run_id)
                yield failed_event(run_id)
                return
            else:
                status = "pending"
            yield ServerSentEvent(
                id=str(run_id),
                event="status",
                data=ChatStatusData(run_id=run_id, status=status),
            )
            await asyncio.sleep(1)

    async for event in events():
        yield event
