import asyncio
from types import SimpleNamespace
from uuid import uuid4

import pytest
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient
from temporalio.exceptions import ApplicationError
from temporalio.service import RPCError, RPCStatusCode

import app.chat_api as chat_api
import app.chat_cancellation as cancellation
from app.chat_dispatcher import ChatActivities, dispatch_chats
from app.chat_repository import ChatRepository
from app.errors import DomainError


@pytest.mark.parametrize("initial", ["pending", "cancelling", "cancelled", "completed", "failed"])
@pytest.mark.parametrize("queued", [False, True])
def test_cancel_endpoint_is_idempotent_and_preserves_finished_answers(monkeypatch, initial, queued):
    program_id, session_id, turn_id = [uuid4() for _ in range(3)]
    state = {"id": turn_id, "status": initial, "workflow_id": f"chat-{turn_id}"}
    terminations = []

    def request_cancel(*ids):
        assert ids == (program_id, session_id, turn_id)
        if state["status"] == "pending":
            state["status"] = "cancelling"
        return dict(state)

    def finish_cancel(identity):
        assert identity == turn_id
        state["status"] = "cancelled"

    async def terminate(**kwargs):
        terminations.append(kwargs)
        if queued:
            raise RPCError("Not started", RPCStatusCode.NOT_FOUND, b"")

    repository = SimpleNamespace(
        request_cancel=request_cancel,
        finish_cancel=finish_cancel,
        turn_status=lambda _: state,
    )
    monkeypatch.setattr(chat_api, "repo", lambda _: repository)
    app = FastAPI()
    app.include_router(chat_api.router)
    app.state.resources = SimpleNamespace(temporal=SimpleNamespace(
        get_workflow_handle=lambda identity: (
            SimpleNamespace(terminate=terminate) if identity == state["workflow_id"] else None
        ),
    ))
    with TestClient(app) as client:
        path = f"/api/v1/programs/{program_id}/chat/sessions/{session_id}/runs/{turn_id}/cancel"
        first = client.post(path)
        second = client.post(path)

    expected = "cancelled" if initial in {"pending", "cancelling", "cancelled"} else initial
    assert first.status_code == second.status_code == 202
    assert first.json() == second.json() == {
        "id": str(turn_id), "run_id": str(turn_id), "status": expected,
    }
    assert len(terminations) == (1 if initial in {"pending", "cancelling"} else 0)


def test_cancel_checks_ownership_before_contacting_temporal(monkeypatch):
    def request_cancel(*_):
        raise DomainError(404, "Ход чата не найден")

    monkeypatch.setattr(chat_api, "repo", lambda _: SimpleNamespace(request_cancel=request_cancel))
    app = FastAPI()
    app.include_router(chat_api.router)

    @app.exception_handler(DomainError)
    async def domain_error(request: Request, error: DomainError):
        return JSONResponse({"detail": error.detail}, status_code=error.status)

    with TestClient(app) as client:
        response = client.post(
            f"/api/v1/programs/{uuid4()}/chat/sessions/{uuid4()}/runs/{uuid4()}/cancel"
        )
    assert response.status_code == 404


@pytest.mark.parametrize("failure", ["unavailable", "timeout"])
def test_cancel_failure_keeps_intent_for_dispatcher_retry(monkeypatch, failure):
    turn = {"id": uuid4(), "workflow_id": "chat-run"}
    finished = []
    calls = []
    monkeypatch.setattr(cancellation, "DELIVERY_TIMEOUT_SECONDS", 0.01)

    async def terminate(**kwargs):
        calls.append(kwargs)
        if len(calls) == 1:
            if failure == "unavailable":
                raise RPCError("Unavailable", RPCStatusCode.UNAVAILABLE, b"")
            await asyncio.sleep(10)

    client = SimpleNamespace(get_workflow_handle=lambda _: SimpleNamespace(terminate=terminate))
    repo = SimpleNamespace(finish_cancel=finished.append)

    async def run():
        await cancellation.cancel_chat(client, repo, turn)
        assert finished == []
        await cancellation.cancel_chat(client, repo, turn)

    asyncio.run(run())
    assert finished == [turn["id"]]


def test_dispatcher_retries_cancel_before_starting_another_turn():
    cancelled_id, next_id = uuid4(), uuid4()
    calls = []
    stop = asyncio.Event()
    finished = []

    async def terminate(**_):
        calls.append("terminate")

    async def start_workflow(*_, **kwargs):
        assert finished == [cancelled_id]
        calls.append(kwargs["id"])
        stop.set()

    repo = SimpleNamespace(
        cancellations_pending=lambda: [{"id": cancelled_id, "workflow_id": "old"}],
        finish_cancel=finished.append,
        pending=lambda *args: [(next_id, "new", None)] if not stop.is_set() else [],
        turn_input=lambda _: {
            "status": "pending", "session_id": uuid4(), "program_id": uuid4(),
            "version_id": uuid4(), "user_text": "Следующий запрос",
        },
        prior_turns=lambda *args: [],
    )
    client = SimpleNamespace(
        start_workflow=start_workflow,
        get_workflow_handle=lambda _: SimpleNamespace(terminate=terminate),
    )
    asyncio.run(dispatch_chats(client, repo, "chat", stop))
    assert calls == ["terminate", "new"]


@pytest.mark.parametrize("status", ["cancelling", "cancelled", "failed", "completed"])
def test_late_workflow_start_cannot_call_model(status):
    activities = ChatActivities(SimpleNamespace(turn_status=lambda _: {"status": status}))
    with pytest.raises(ApplicationError) as error:
        asyncio.run(activities.mark_running(str(uuid4())))
    assert error.value.non_retryable


def test_sse_returns_cancelled_from_saved_history_without_temporal(monkeypatch):
    program_id, run_id = uuid4(), uuid4()
    repository = SimpleNamespace(
        turn_input=lambda _: {"program_id": program_id, "session_id": uuid4()},
        session=lambda *_: {"turns": [{"id": run_id, "status": "cancelled", "result": None}]},
    )
    monkeypatch.setattr(chat_api, "repo", lambda _: repository)
    app = FastAPI()
    app.include_router(chat_api.router)
    with TestClient(app) as client:
        response = client.get(f"/api/v1/programs/{program_id}/chat/runs/{run_id}/events")
    assert response.status_code == 200
    assert "event: cancelled" in response.text
    assert "event: failed" not in response.text


def test_sse_keeps_main_progress_and_reports_cancellation(monkeypatch):
    from temporalio.client import WorkflowExecutionStatus

    program_id, run_id = uuid4(), uuid4()
    states = iter(["pending", "cancelled"])

    async def describe(**kwargs):
        return SimpleNamespace(status=WorkflowExecutionStatus.RUNNING)

    async def query(*args, **kwargs):
        return {"message": "Читаю документ…", "steps": []}

    repository = SimpleNamespace(
        turn_input=lambda _: {"program_id": program_id, "session_id": uuid4()},
        session=lambda *_: {"turns": [{"id": run_id, "status": next(states), "result": None}]},
    )
    monkeypatch.setattr(chat_api, "repo", lambda _: repository)
    app = FastAPI()
    app.include_router(chat_api.router)
    app.state.resources = SimpleNamespace(temporal=SimpleNamespace(
        get_workflow_handle=lambda _: SimpleNamespace(describe=describe, query=query),
    ))
    with TestClient(app) as client:
        response = client.get(f"/api/v1/programs/{program_id}/chat/runs/{run_id}/events")
    assert response.status_code == 200
    assert "event: progress" in response.text
    assert "Читаю документ…" in response.text
    assert "event: cancelled" in response.text


@pytest.mark.parametrize("status", ["cancelling", "cancelled"])
def test_late_result_is_discarded_before_reading_graph(status):
    repository = ChatRepository(None)
    repository.turn_input = lambda _: {"status": status}
    repository.complete(uuid4(), {"kind": "answer", "message": "Поздний ответ"}, b"{}")
