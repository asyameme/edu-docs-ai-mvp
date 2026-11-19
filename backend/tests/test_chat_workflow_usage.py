"""Проверки области действия лимитов использования чата."""

from pydantic_ai import Conversation
from pydantic_ai.usage import RunUsage

from app.chat_workflow import _conversation_for_turn


def test_usage_limits_restart_for_new_turn_without_losing_conversation_identity():
    previous = Conversation(
        usage=RunUsage(requests=8, tool_calls=12, input_tokens=32000),
        conversation_id="existing-conversation",
    )

    current = _conversation_for_turn(previous)

    assert current.usage == RunUsage()
    assert current.conversation_id == previous.conversation_id
    assert current.messages == previous.messages
