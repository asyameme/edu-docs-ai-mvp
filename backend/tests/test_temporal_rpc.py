"""Дедлайны RPC и повторная доставка после неизвестного результата старта."""

import asyncio
import importlib
from types import SimpleNamespace
from uuid import uuid4

import pytest
from temporalio.client import WorkflowExecutionStatus
from temporalio.exceptions import WorkflowAlreadyStartedError

import app.temporal_rpc as rpc
from app.errors import DomainError
from app.generation_api import generation_status
from app.import_status import read_status
from app.review_api import job_state


@pytest.mark.parametrize("kind,phase", [
    ("import", "describe"), ("import", "query"), ("review", "describe"),
    ("review", "query"), ("generation", "describe"),
])
def test_status_deadline_returns_503_and_cancels_call(monkeypatch, kind, phase):
    monkeypatch.setattr(rpc, "STATUS_TIMEOUT_SECONDS", 0.02)
    cancelled = []

    async def delayed():
        try:
            await asyncio.sleep(10)
        finally:
            cancelled.append(True)

    async def describe(**kwargs):
        if phase == "describe":
            return await delayed()
        return SimpleNamespace(status=WorkflowExecutionStatus.RUNNING)

    async def query(*args, **kwargs):
        return await delayed()

    handle = SimpleNamespace(describe=describe, query=query)
    temporal = SimpleNamespace(namespace="test", get_workflow_handle=lambda _: handle)
    if kind == "import":
        stored = {
            "status": "pending",
            "_terminal": False,
            "_namespace": "test",
            "_workflow_id": "test",
            "_legacy": False,
        }
        operation = read_status(SimpleNamespace(read=lambda *_: stored), temporal, "p", "j")
    elif kind == "generation":
        stored = {"status": "pending", "_namespace": "test", "_workflow_id": "test"}
        operation = generation_status(SimpleNamespace(job=lambda *_: stored), temporal, "p", "j")
    else:
        stored = {
            "id": "j",
            "parameters": {"review_id": "r", "purpose": "check"},
            "report": {},
            "input_version_id": "v",
            "output_version_id": None,
            "temporal_namespace": "test",
            "workflow_id": "test",
        }
        operation = job_state(SimpleNamespace(job=lambda *_: stored), temporal, "p", "j")
    with pytest.raises(DomainError) as error:
        asyncio.run(operation)
    assert error.value.status == 503
    assert cancelled == [True]
    assert stored.get("status", "pending") == "pending"


@pytest.mark.parametrize("kind", ["import", "review", "graph_build", "generation", "chat"])
def test_delivery_retries_same_workflow_after_timeout_and_stops(monkeypatch, kind):
    module = importlib.import_module(f"app.{kind}_dispatcher")
    monkeypatch.setattr(module, "DELIVERY_TIMEOUT_SECONDS", 0.02)
    identity = uuid4()
    workflow_id = f"import-{identity}-1" if kind == "import" else f"{kind}-{identity}"
    record = (identity, workflow_id)
    if kind == "import":
        record += (False,)
    elif kind == "chat":
        record += (None,)
    stop_event = asyncio.Event()
    attempts, cancellations = [], []

    class Stop:
        def is_set(self):
            return stop_event.is_set()

        async def wait(self):
            # Повторяем доставку сразу, без двухсекундной паузы теста.
            return None

    class Client:
        async def start_workflow(self, *args, **kwargs):
            attempts.append(kwargs["id"])
            if len(attempts) == 1:
                try:
                    await asyncio.sleep(10)
                finally:
                    cancellations.append(True)
            stop_event.set()
            # Сервер мог принять первый старт до потери ответа.
            raise WorkflowAlreadyStartedError(workflow_id, "TestWorkflow")

        def get_workflow_handle(self, _):
            async def describe():
                return SimpleNamespace(status=WorkflowExecutionStatus.RUNNING)

            return SimpleNamespace(describe=describe)

    def must_not_fail(*args):
        raise AssertionError("Повтор доставки не должен завершать задание ошибкой")

    repo = SimpleNamespace(
        pending=lambda *args: [record] if not stop_event.is_set() else [],
        graph_build_pending=lambda: [record] if not stop_event.is_set() else [],
        turn_input=lambda _: {
            "session_id": uuid4(),
            "program_id": uuid4(),
            "version_id": uuid4(),
            "user_text": "Вопрос",
        },
        prior_turns=lambda *args: [],
        fail=must_not_fail,
    )
    functions = {
        "import": "dispatch_imports",
        "review": "dispatch_reviews",
        "graph_build": "dispatch_graph_builds",
        "generation": "dispatch_generations",
        "chat": "dispatch_chats",
    }

    async def run():
        await asyncio.wait_for(getattr(module, functions[kind])(Client(), repo, "queue", Stop()), 1)

    asyncio.run(run())
    assert attempts == [workflow_id, workflow_id]
    assert cancellations == [True]
