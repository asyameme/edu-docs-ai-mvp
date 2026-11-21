"""Выполнение одного хода Pydantic AI с устойчивыми шагами Temporal."""

from dataclasses import replace
from datetime import timedelta

from pydantic_ai import Conversation, ConversationTypeAdapter, UsageLimits
from pydantic_ai.durable_exec.temporal import PydanticAIWorkflow
from pydantic_ai.messages import ModelRequest, UserPromptPart
from pydantic_ai.usage import RunUsage
from temporalio import workflow
from temporalio.common import RetryPolicy

with workflow.unsafe.imports_passed_through():
    from app.chat_model import chat_agent
    from worker.agent.contracts import ChatDeps

MAX_CHAT_HISTORY_TURNS = 2
MAX_CHAT_HISTORY_BYTES = 32_000


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
            result = await chat_agent.run(
                payload["text"],
                deps=ChatDeps(program_id=payload["program_id"], version_id=payload["version_id"]),
                conversation=conversation,
                usage_limits=UsageLimits(
                    request_limit=8,
                    tool_calls_limit=12,
                    input_tokens_limit=32000,
                    output_tokens_limit=4000,
                    total_tokens_limit=36000,
                ),
            )
            serialized = ConversationTypeAdapter.dump_json(result.conversation).decode()
            output = result.output.model_dump(mode="json")
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
