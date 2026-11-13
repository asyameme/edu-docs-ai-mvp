"""Создание агента с моделью OpenAI-compatible API из настроек окружения."""

from openai import AsyncOpenAI
from pydantic_ai.models.openai import OpenAIChatModel
from pydantic_ai.providers.openai import OpenAIProvider

from app.config import get_settings
from worker.agent.chat_agent import create_chat_agent

settings = get_settings()
if settings.validate_chat_worker_config():
    model = OpenAIChatModel(
        settings.chat_model,
        provider=OpenAIProvider(
            openai_client=AsyncOpenAI(
                base_url=settings.chat_base_url,
                api_key=settings.chat_api_key.get_secret_value(),
                timeout=settings.chat_model_timeout_seconds,
            ),
        ),
    )
    chat_agent = create_chat_agent(model)
else:
    chat_agent = None
