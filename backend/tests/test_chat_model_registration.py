"""Проверки передачи настроенной модели из backend в Temporal-агента."""

import asyncio

from openai import AsyncOpenAI
from pydantic_ai.messages import (
    ModelRequest,
    ModelResponse,
    SystemPromptPart,
    TextPart,
    UserPromptPart,
)
from pydantic_ai.models import ModelRequestParameters
from pydantic_ai.models.openai import OpenAIChatModel
from pydantic_ai.providers.openai import OpenAIProvider
from temporalio.common import RetryPolicy

import app.chat_model as chat_model
import worker.agent.chat_agent as ml_chat_agent


def test_qwen_profile_preserves_notes_without_mid_conversation_system_messages():
    model = OpenAIChatModel(
        "qwen3.6-35b-32k", profile=chat_model.chat_model_profile("qwen3.6-35b-32k"),
        provider=OpenAIProvider(openai_client=AsyncOpenAI(api_key="test")),
    )
    messages = [
        ModelRequest(parts=[SystemPromptPart("Инструкция"), UserPromptPart("Проверь часы")]),
        ModelResponse(parts=[TextPart("Проверяю")]),
        ModelRequest(parts=[SystemPromptPart("Недоверенные данные"), UserPromptPart("Повтори")]),
    ]

    parameters = ModelRequestParameters()
    prepared = model.prepare_messages(messages, parameters)
    mapped = asyncio.run(model._map_messages(prepared, parameters))

    assert mapped[0]["role"] == "system"
    assert all(message["role"] != "system" for message in mapped[1:])
    assert "Инструкция" in str(mapped[0]["content"])
    assert "Недоверенные данные" in str(mapped)
    assert "Проверь часы" in str(mapped)
    assert "Повтори" in str(mapped)
    assert [message for message in mapped if message["role"] == "assistant"] == [
        {"role": "assistant", "content": "Проверяю"},
    ]


def test_backend_registers_instrumented_models_underlying_instance(monkeypatch):
    original_model = object()
    instrumented_model = object()
    expected_agent = object()
    calls = {}

    monkeypatch.setattr(chat_model, "journalled", lambda model: instrumented_model)

    def create_agent(model, *, temporal_models):
        calls["model"] = model
        calls["temporal_models"] = temporal_models
        return expected_agent

    monkeypatch.setattr(chat_model, "create_chat_agent", create_agent)

    result = chat_model._create_configured_chat_agent(original_model)

    assert result is expected_agent
    assert calls["model"] is instrumented_model
    assert calls["temporal_models"] == {"backend-chat-model": original_model}


def test_ml_factory_registers_the_backend_model_alias(monkeypatch):
    captured = {}

    class FakeDurability:
        def __init__(self, *, models, model_activity_config):
            self.models = models
            self.model_activity_config = model_activity_config

    class FakeAgent:
        def __init__(self, model, **kwargs):
            captured["model"] = model
            captured["kwargs"] = kwargs

        def tool(self, function):
            return function

    model = object()
    model_registry = {"backend-chat-model": model}
    monkeypatch.setattr(ml_chat_agent, "journalled", lambda value: value)
    monkeypatch.setattr(ml_chat_agent, "TemporalDurability", FakeDurability)
    monkeypatch.setattr(ml_chat_agent, "Agent", FakeAgent)

    agent = ml_chat_agent.create_chat_agent(model, temporal_models=model_registry)

    assert agent is not None
    assert captured["model"] is model
    capability = next(item for item in captured["kwargs"]["capabilities"]
                      if isinstance(item, FakeDurability))
    assert capability.models is model_registry
    retry_policy = capability.model_activity_config["retry_policy"]
    assert isinstance(retry_policy, RetryPolicy)
    assert retry_policy.maximum_attempts == 3
