"""Золотые прогоны агента на живой модели (Qwen через LiteLLM-прокси ментора).

В CI и обычном прогоне пропускаются: нужен ключ. Локальный запуск из backend/:

    CHAT_API_KEY=... CHAT_BASE_URL=https://llm.ai-expert-opinion.ru/v1 \
    CHAT_MODEL=qwen3.6-35b-32k ../../mvp_proj/.venv/bin/python -m pytest tests/test_agent_live.py -q

Ключ и адрес те же, что у валидатора (VALIDATION_LLM_API_KEY/VALIDATION_LLM_API_BASE
в .env). Имя модели — без litellm-префикса ``openai/``.
"""

import asyncio
import os
from uuid import uuid4

import pytest
from test_agent_behavior import FakeReadPort

from worker.agent import read_port as read_port_module
from worker.agent.chat_agent import create_chat_agent
from worker.agent.contracts import Answer, ChangeProposal, ChatDeps, Clarification

pytestmark = pytest.mark.skipif(
    not (os.environ.get("CHAT_API_KEY") or os.environ.get("VALIDATION_LLM_API_KEY")),
    reason="нет ключа LLM: задайте CHAT_API_KEY или VALIDATION_LLM_API_KEY",
)


def _live_model():
    from openai import AsyncOpenAI
    from pydantic_ai.models.openai import OpenAIChatModel
    from pydantic_ai.providers.openai import OpenAIProvider

    name = (
        os.environ.get("CHAT_MODEL") or os.environ.get("VALIDATION_LLM_MODEL", "qwen3.6-35b-32k")
    ).split("/")[-1]
    base = os.environ.get("CHAT_BASE_URL") or os.environ.get("VALIDATION_LLM_API_BASE")
    api_key = os.environ.get("CHAT_API_KEY") or os.environ["VALIDATION_LLM_API_KEY"]
    return OpenAIChatModel(
        name,
        provider=OpenAIProvider(
            openai_client=AsyncOpenAI(base_url=base, api_key=api_key, timeout=90.0)
        ),
    )


@pytest.fixture
def fake_port(monkeypatch):
    port = FakeReadPort()
    monkeypatch.setattr(read_port_module, "_worker_read_port", port)
    return port


def _run(prompt):
    agent = create_chat_agent(_live_model())
    return asyncio.run(
        agent.run(prompt, deps=ChatDeps(program_id=uuid4(), version_id=uuid4()))
    )


def test_live_answer_uses_tools(fake_port):
    result = _run("Какая трудоёмкость у дисциплины по MLOps?")
    assert isinstance(result.output, Answer)
    assert "108" in result.output.message
    assert fake_port.calls


def test_live_vague_request_clarifies(fake_port):
    result = _run("Поменяй дисциплину")
    assert isinstance(result.output, Clarification)
    assert result.output.questions


def test_live_change_request_returns_proposal(fake_port):
    result = _run("Сократи трудоёмкость MLOps со 108 до 54 часов")
    assert isinstance(result.output, ChangeProposal)
    assert result.output.changes[0].new_value == 54
    assert result.output.changes[0].expected_old_value == 108
