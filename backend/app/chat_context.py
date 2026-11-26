"""Приблизительный бюджет контекста и целые обмены без ограничения их числа."""

from functools import lru_cache

import tiktoken
from pydantic_ai.exceptions import UsageLimitExceeded
from pydantic_ai.messages import ModelRequest, UserPromptPart
from pydantic_core import to_json

DEFAULT_CONTEXT_TOKENS = 200_000
CONTEXT_RESERVE_TOKENS = 20_000
MAX_HISTORY_BYTES = 1_200_000


@lru_cache(maxsize=1)
def _encoding():
    return tiktoken.get_encoding("cl100k_base")


def count_text_tokens(text):
    return len(_encoding().encode(text, disallowed_special=()))


def count_message_tokens(messages):
    # Сериализация учитывает роли и служебные поля; это оценка, не токенизатор Qwen.
    return count_text_tokens(to_json(messages).decode())


def trim_messages(messages, token_budget, *, max_bytes=MAX_HISTORY_BYTES, allow_empty=False):
    """Удаляет старые обмены целиком, не разрывая вызов инструмента и его ответ."""
    starts = [i for i, message in enumerate(messages)
              if isinstance(message, ModelRequest)
              and any(isinstance(part, UserPromptPart) for part in message.parts)]
    if not starts:
        if count_message_tokens(messages) > token_budget or len(to_json(messages)) > max_bytes:
            raise UsageLimitExceeded("Текущий контекст не помещается в бюджет")
        return messages
    ends = [*starts[1:], len(messages)]
    groups = [messages[start:end] for start, end in zip(starts, ends, strict=True)]
    kept, tokens, size = [], 0, 0
    for group in reversed(groups):
        group_tokens = count_message_tokens(group)
        group_bytes = len(to_json(group))
        if tokens + group_tokens > token_budget or size + group_bytes > max_bytes:
            if not kept and not allow_empty:
                raise UsageLimitExceeded("Последний обмен не помещается в бюджет контекста")
            break
        kept.append(group)
        tokens += group_tokens
        size += group_bytes
    return [message for group in reversed(kept) for message in group]
