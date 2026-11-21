"""Проверки области действия лимитов использования чата."""

from pydantic_ai import Conversation
from pydantic_ai.messages import ModelRequest, ModelResponse, TextPart, UserPromptPart
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


def _exchange(text: str) -> list[ModelRequest | ModelResponse]:
    return [
        ModelRequest(parts=[UserPromptPart(content=text)]),
        ModelResponse(parts=[TextPart(content=f"ответ: {text}")]),
    ]


def test_conversation_keeps_only_the_two_most_recent_user_turns():
    previous = Conversation(
        messages=[*_exchange("старый"), *_exchange("недавний 1"), *_exchange("недавний 2")],
        conversation_id="existing-conversation",
    )

    current = _conversation_for_turn(previous)

    assert len(current.messages) == 4
    assert current.messages[0].parts[0].content == "недавний 1"
    assert current.messages[2].parts[0].content == "недавний 2"


def test_conversation_drops_old_exchange_when_serialized_history_is_too_large():
    previous = Conversation(
        messages=[*_exchange("старый " + "x" * 24_000), *_exchange("сохрани этот контекст")],
        conversation_id="existing-conversation",
    )

    current = _conversation_for_turn(previous)

    assert len(current.messages) == 2
    assert current.messages[0].parts[0].content == "сохрани этот контекст"
