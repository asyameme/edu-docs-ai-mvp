"""Проверки области действия лимитов использования чата."""

from pydantic_ai import Conversation
from pydantic_ai.messages import ModelRequest, ModelResponse, TextPart, UserPromptPart
from pydantic_ai.usage import RunUsage

from app.chat_workflow import MAX_CHAT_HISTORY_BYTES, _conversation_for_turn, conversation_from_turns


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


def test_saved_clarification_survives_large_tool_history():
    text = "В «Глубоком обучении» оставим 154 часа"
    question = "В каком семестре изменить часы, в пятом, шестом или обоих?"
    conversation = conversation_from_turns("session", [{
        "user_text": text,
        "result": {"kind": "clarification", "message": "Нашёл две строки", "questions": [question]},
        "conversation": b"x" * (MAX_CHAT_HISTORY_BYTES + 1),
    }])

    request, response = conversation.messages
    assert request.parts[0].content == text
    assert question in response.parts[0].content


def test_latest_exchange_survives_when_its_visible_text_is_oversized():
    conversation = conversation_from_turns("session", [{
        "user_text": "Глубокое обучение " + "я" * MAX_CHAT_HISTORY_BYTES + " 154 часа",
        "result": {
            "kind": "clarification",
            "message": "я" * MAX_CHAT_HISTORY_BYTES,
            "questions": ["Меняем оба семестра?"],
        },
    }])

    request, response = conversation.messages
    assert request.parts[0].content.startswith("Глубокое обучение")
    assert request.parts[0].content.endswith("154 часа")
    assert "Меняем оба семестра?" in response.parts[0].content
