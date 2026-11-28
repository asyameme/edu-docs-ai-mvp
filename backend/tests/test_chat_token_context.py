"""Контекст ограничивается токенами и сохраняет целые обмены."""

import pytest
from pydantic_ai.exceptions import UsageLimitExceeded
from pydantic_ai.messages import ModelRequest, ModelResponse, TextPart, UserPromptPart
from pydantic_core import to_json

from app.chat_context import count_message_tokens, trim_messages


def exchange(text):
    return [ModelRequest(parts=[UserPromptPart(content=text)]),
            ModelResponse(parts=[TextPart(content="Ответ: " + text)])]


def test_short_conversation_keeps_more_than_ten_exchanges():
    messages = [m for i in range(50) for m in exchange(f"Вопрос {i}")]
    assert trim_messages(messages, 180_000) == messages


def test_token_budget_drops_whole_old_exchanges():
    recent = exchange("Нужный контекст")
    messages = exchange("Старый контекст " * 100) + recent
    assert trim_messages(messages, count_message_tokens(recent)) == recent


def test_latest_exchange_cannot_be_silently_cut():
    with pytest.raises(UsageLimitExceeded):
        trim_messages(exchange("длинный текст " * 100), 20)


def test_token_count_handles_special_token_text_as_ordinary_user_text():
    assert count_message_tokens(exchange("<|endoftext|>")) > 0


def test_large_history_is_not_cut_at_previous_byte_limit():
    messages = [m for i in range(30) for m in exchange(f"Вопрос {i} " + "текст " * 1000)]
    assert trim_messages(messages, 180_000) == messages


def test_transport_limit_keeps_recent_exchange_whole():
    recent = exchange("Нужный контекст")
    budget = len(to_json(recent)) + 1
    assert trim_messages(exchange("a " * 1000) + recent, 180_000, max_bytes=budget) == recent


def test_agent_context_processor_preserves_current_tool_call_pairs():
    from pydantic_ai.messages import ToolCallPart, ToolReturnPart

    current = [ModelRequest(parts=[UserPromptPart(content="Текущий вопрос")]),
               ModelResponse(parts=[ToolCallPart("read_entity", {}, tool_call_id="call-1")]),
               ModelRequest(parts=[ToolReturnPart("read_entity", "Данные", tool_call_id="call-1")])]
    messages = exchange("Старый текст " * 100) + current
    assert trim_messages(messages, count_message_tokens(current)) == current


def test_visible_history_remembers_unresolved_questions():
    from app.chat_workflow import token_conversation_from_turns

    conversation = token_conversation_from_turns("session", [{
        "user_text": "Измени часы", "result": {"kind": "clarification",
        "message": "Нужны данные", "questions": ["Какой семестр?"]},
    }], 180_000)
    assert "Какой семестр?" in conversation.messages[-1].parts[0].content


def test_real_agent_invokes_token_processor_and_retains_long_visible_history():
    import asyncio
    from uuid import uuid4

    from pydantic_ai import Conversation
    from pydantic_ai.models.test import TestModel

    from worker.agent.chat_agent import create_chat_agent
    from worker.agent.contracts import ChatDeps

    agent = create_chat_agent(TestModel(call_tools=[], custom_output_args={
        "kind": "answer", "message": "Помню контекст", "sources": [],
    }))
    messages = [m for i in range(25) for m in exchange(f"Сообщение {i}")]
    result = asyncio.run(agent.run("Продолжаем", conversation=Conversation(messages=messages),
        deps=ChatDeps(program_id=uuid4(), version_id=uuid4(), context_tokens=200_000)))
    assert result.output.message == "Помню контекст"
    assert len(result.conversation.messages) >= 52


def test_history_can_be_empty_when_current_question_needs_the_budget():
    assert trim_messages(exchange("Старое сообщение " * 100), 20, allow_empty=True) == []


def test_history_uses_nearly_the_token_budget_without_a_turn_count_cutoff():
    messages = [m for i in range(120) for m in exchange(f"Вопрос {i} " + "обычный текст " * 1000)]
    kept = trim_messages(messages, 180_000)
    assert 160_000 < count_message_tokens(kept) <= 180_000
    assert len(kept) > 20
    assert kept[-1] == messages[-1]


def test_dispatcher_loads_more_than_one_history_page():
    import asyncio
    from uuid import uuid4

    from pydantic_ai import ConversationTypeAdapter

    from app.chat_dispatcher import dispatch_chats

    async def run():
        stop = asyncio.Event()
        turns = [{"user_text": f"Вопрос {i}", "result": {
            "kind": "answer", "message": f"Ответ {i}", "sources": [],
        }} for i in range(105)]
        offsets, payloads = [], []

        class Repo:
            def cancellations_pending(self):
                return []

            def pending(self, after=None):
                return [(uuid4(), "workflow", None)] if after is None else []

            def turn_input(self, turn_id):
                return {"status": "pending", "session_id": uuid4(),
                        "program_id": uuid4(), "version_id": uuid4(),
                        "user_text": "Продолжаем"}

            def prior_turns(self, turn, limit, offset=0):
                offsets.append(offset)
                return list(reversed(list(reversed(turns))[offset:offset + limit]))

        class Client:
            async def start_workflow(self, _, payload, **kwargs):
                payloads.append(payload)
                stop.set()

        await dispatch_chats(Client(), Repo(), "queue", stop)
        conversation = ConversationTypeAdapter.validate_json(payloads[0]["conversation"])
        assert offsets == [0, 100]
        assert len(conversation.messages) == 210
        assert conversation.messages[0].parts[0].content == "Вопрос 0"
        assert conversation.messages[-2].parts[0].content == "Вопрос 104"
        assert payloads[0]["context_tokens"] == 200_000

    asyncio.run(run())


def test_qwen_retry_does_not_insert_system_messages_after_user_history():
    import asyncio
    import json
    from uuid import uuid4

    import httpx
    from openai import AsyncOpenAI
    from pydantic_ai import Conversation
    from pydantic_ai.models.openai import OpenAIChatModel
    from pydantic_ai.providers.openai import OpenAIProvider

    from app.chat_model import chat_model_profile
    from worker.agent.chat_agent import create_chat_agent
    from worker.agent.contracts import ChatDeps

    requests = []

    def respond(request):
        body = json.loads(request.content)
        requests.append(body)
        if len(requests) == 1:
            message = {"role": "assistant", "content": "продолжаем"}
        else:
            answer = next(tool["function"]["name"] for tool in body["tools"]
                          if tool["function"]["parameters"].get("properties", {})
                          .get("kind", {}).get("const") == "answer")
            message = {"role": "assistant", "tool_calls": [{"id": "answer-1", "type": "function",
                "function": {"name": answer, "arguments": json.dumps({
                    "kind": "answer", "message": "Продолжаем", "sources": [],
                })}}]}
        return httpx.Response(200, json={
            "id": "response", "model": "qwen", "object": "chat.completion",
            "choices": [{"index": 0, "finish_reason": "stop", "message": message}],
            "usage": {"prompt_tokens": 100, "completion_tokens": 10, "total_tokens": 110}})

    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as http:
            model = OpenAIChatModel("qwen-test", profile=chat_model_profile("qwen-test"),
                provider=OpenAIProvider(openai_client=AsyncOpenAI(
                    api_key="test", base_url="https://example.invalid/v1", http_client=http)))
            result = await create_chat_agent(model).run("Продолжаем",
                conversation=Conversation(messages=exchange("Старое сообщение")),
                deps=ChatDeps(program_id=uuid4(), version_id=uuid4(), context_tokens=200_000))
            assert result.output.message == "Продолжаем"

    asyncio.run(run())
    assert len(requests) == 2
    for request in requests:
        roles = [message["role"] for message in request["messages"]]
        assert "system" not in roles[1:]
