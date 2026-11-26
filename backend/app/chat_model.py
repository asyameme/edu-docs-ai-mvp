"""Создание агента с моделью OpenAI-compatible API из настроек окружения."""

from openai import AsyncOpenAI
from pydantic_ai import Agent
from pydantic_ai.models import Model
from pydantic_ai.models.openai import OpenAIChatModel
from pydantic_ai.profiles.openai import OpenAIModelProfile
from pydantic_ai.providers.openai import OpenAIProvider

from app.chat_validation import register_chat_validation_tools
from app.config import get_settings
from worker.agent.chat_agent import create_chat_agent
from worker.agent.contracts import ChatDeps, ChatOutput
from worker.agent.llm_journal import journalled

settings = get_settings()


def chat_model_profile(model_name: str) -> OpenAIModelProfile:
    """Qwen принимает системную роль только в начале, включая запросы повторной попытки."""
    if "qwen" in model_name.lower():
        return OpenAIModelProfile(
            supports_inline_system_prompts=False,
            openai_chat_supports_multiple_system_messages=False,
        )
    return OpenAIModelProfile()


def _create_configured_chat_agent(model: Model) -> Agent[ChatDeps, ChatOutput]:
    """Создаёт ML-агента и регистрирует ту же журналируемую модель для Temporal."""

    journalled_model = journalled(model)
    return create_chat_agent(
        journalled_model,
        temporal_models={"backend-chat-model": model},
    )


if settings.validate_chat_worker_config():
    model = OpenAIChatModel(
        settings.chat_model,
        profile=chat_model_profile(settings.chat_model),
        provider=OpenAIProvider(
            openai_client=AsyncOpenAI(
                base_url=settings.chat_base_url,
                api_key=settings.chat_api_key.get_secret_value(),
                timeout=settings.chat_model_timeout_seconds,
            ),
        ),
    )
    chat_agent = _create_configured_chat_agent(model)
    register_chat_validation_tools(chat_agent)
else:
    chat_agent = None
