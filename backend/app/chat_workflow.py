"""Выполнение одного хода Pydantic AI с устойчивыми шагами Temporal."""

import json
from dataclasses import replace
from datetime import timedelta

from pydantic_ai import Conversation, ConversationTypeAdapter, UsageLimits
from pydantic_ai.durable_exec.temporal import PydanticAIWorkflow
from pydantic_ai.exceptions import UsageLimitExceeded
from pydantic_ai.messages import ModelRequest, ModelResponse, TextPart, UserPromptPart
from pydantic_ai.usage import RunUsage
from pydantic_core import to_json
from temporalio import workflow
from temporalio.common import RetryPolicy

with workflow.unsafe.imports_passed_through():
    from app.chat_model import chat_agent
    from worker.agent.contracts import ChatDeps

MAX_CHAT_HISTORY_TURNS = 2
MAX_CHAT_HISTORY_BYTES = 32_000
MAX_CHAT_REQUEST_INPUT_TOKENS = 32_000
# На каждом шаге модель заново получает часть контекста. Бюджет всего хода
# учитывает эти повторы отдельно от размера одного запроса.
MAX_CHAT_TURN_INPUT_TOKENS = 128_000
CHAT_LIMIT_MESSAGE = (
    "Запрос потребовал слишком много данных. Уточните, какую дисциплину, "
    "семестр или поле нужно проверить или изменить."
)


def _bounded_history_text(text: str) -> str:
    # Даже один большой обмен должен помещаться целиком как пара сообщений.
    budget = MAX_CHAT_HISTORY_BYTES // 3
    if len(to_json(text)) <= budget:
        return text
    low, high = 0, len(text)
    best = "[сокращено]"
    while high - low > 1:
        length = (low + high) // 2
        start, end = length // 2, (length + 1) // 2
        excerpt = text[:start] + "\n[сокращено]\n" + text[-end:]
        if len(to_json(excerpt)) <= budget:
            low = length
            best = excerpt
        else:
            high = length
    return best


def conversation_from_turns(session_id, turns) -> Conversation:
    """Воспроизводит видимые сообщения без ответов инструментов и рассуждений модели."""
    messages = []
    for turn in turns:
        result = turn["result"]
        if result["kind"] == "answer":
            result = {
                **result,
                "sources": [{"id": item["id"]} for item in result.get("sources", [])],
            }
        messages.extend([
            ModelRequest(parts=[UserPromptPart(content=_bounded_history_text(turn["user_text"]))]),
            ModelResponse(parts=[TextPart(content=_bounded_history_text(
                json.dumps(result, ensure_ascii=False, separators=(",", ":"))
            ))]),
        ])
    return _conversation_for_turn(Conversation(conversation_id=str(session_id), messages=messages))


def _conversation_for_turn(conversation: Conversation) -> Conversation:
    """Ограничивает контекст чата, сохраняя последние законченные обмены."""
    messages = conversation.messages
    user_message_indexes = [
        index
        for index, message in enumerate(messages)
        if isinstance(message, ModelRequest)
        and any(isinstance(part, UserPromptPart) for part in message.parts)
    ]
    if len(user_message_indexes) > MAX_CHAT_HISTORY_TURNS:
        messages = messages[user_message_indexes[-MAX_CHAT_HISTORY_TURNS] :]

    compacted = replace(conversation, messages=messages, usage=RunUsage(), new_message_index=None)
    while messages and len(ConversationTypeAdapter.dump_json(compacted)) > MAX_CHAT_HISTORY_BYTES:
        user_message_indexes = [
            index
            for index, message in enumerate(messages)
            if isinstance(message, ModelRequest)
            and any(isinstance(part, UserPromptPart) for part in message.parts)
        ]
        if len(user_message_indexes) <= 1:
            messages = []
        else:
            messages = messages[user_message_indexes[1] :]
        compacted = replace(
            compacted, messages=messages, usage=RunUsage(), new_message_index=None
        )
    return compacted


@workflow.defn(name="ProgramChatWorkflowV1")
class ChatWorkflow(PydanticAIWorkflow):
    __pydantic_ai_agents__ = [chat_agent] if chat_agent else []

    @workflow.run
    async def run(self, payload: dict) -> dict:
        if chat_agent is None:
            raise RuntimeError("Чат не настроен: задайте CHAT_MODEL, CHAT_BASE_URL и CHAT_API_KEY")
        turn = payload["turn"]
        try:
            await workflow.execute_activity(
                "chat_mark_running_v1", turn, start_to_close_timeout=timedelta(seconds=20)
            )
            conversation = (
                ConversationTypeAdapter.validate_json(payload["conversation"])
                if payload.get("conversation")
                else Conversation()
            )
            # Conversation хранит usage всего диалога, а лимиты защищают один ход.
            conversation = _conversation_for_turn(conversation)
            context_budget_v2 = workflow.patched("chat-context-budget-v2")
            limits = UsageLimits(
                request_limit=8,
                tool_calls_limit=12,
                input_tokens_limit=MAX_CHAT_TURN_INPUT_TOKENS if context_budget_v2 else 32_000,
                output_tokens_limit=4_000,
                total_tokens_limit=None if context_budget_v2 else 36_000,
                per_request_input_tokens_limit=(
                    MAX_CHAT_REQUEST_INPUT_TOKENS if context_budget_v2 else None
                ),
            )
            try:
                result = await chat_agent.run(
                    payload["text"],
                    deps=ChatDeps(program_id=payload["program_id"], version_id=payload["version_id"]),
                    conversation=conversation,
                    usage_limits=limits,
                )
                conversation = result.conversation
                output = result.output.model_dump(mode="json")
            except UsageLimitExceeded as error:
                if not context_budget_v2:
                    raise
                workflow.logger.warning("Превышен лимит чата: %s", error)
                now = workflow.now()
                conversation = replace(conversation, messages=[
                    *conversation.messages,
                    ModelRequest(parts=[UserPromptPart(content=payload["text"], timestamp=now)]),
                    ModelResponse(parts=[TextPart(content=CHAT_LIMIT_MESSAGE)], timestamp=now),
                ])
                output = {"kind": "answer", "message": CHAT_LIMIT_MESSAGE, "sources": []}
            serialized = ConversationTypeAdapter.dump_json(conversation).decode()
            await workflow.execute_activity(
                "chat_save_result_v1",
                {"turn_id": turn, "result": output, "conversation": serialized},
                start_to_close_timeout=timedelta(seconds=30),
                retry_policy=RetryPolicy(maximum_attempts=5),
            )
        except Exception:
            await workflow.execute_activity(
                "chat_mark_failed_v1",
                turn,
                start_to_close_timeout=timedelta(seconds=20),
                retry_policy=RetryPolicy(maximum_attempts=3),
            )
            raise
        return output
