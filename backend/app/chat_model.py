"""Создание агента с моделью OpenAI-compatible API из настроек окружения."""

from collections.abc import Sequence

from openai import AsyncOpenAI
from openai.types.chat import ChatCompletionMessageParam, ChatCompletionSystemMessageParam
from pydantic_ai import Agent
from pydantic_ai.messages import ModelMessage
from pydantic_ai.models import Model, ModelRequestParameters
from pydantic_ai.models.openai import OpenAIChatModel
from pydantic_ai.profiles.openai import OpenAIModelProfile
from pydantic_ai.providers.openai import OpenAIProvider
from pydantic_ai.settings import ModelSettings

from app.chat_validation import register_chat_validation_tools
from app.config import get_settings
from worker.agent.chat_agent import create_chat_agent
from worker.agent.contracts import AgentOutput, ChatDeps
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


class QwenChatModel(OpenAIChatModel):
    async def _map_messages(
        self, messages: Sequence[ModelMessage], model_request_parameters: ModelRequestParameters,
        *, model_settings: ModelSettings | None = None,
    ) -> list[ChatCompletionMessageParam]:
        mapped = await super()._map_messages(
            messages, model_request_parameters, model_settings=model_settings,
        )
        # При повторной попытке библиотека может добавить системное примечание.
        # Qwen принимает системное сообщение только в начале, в том числе после инструментов.
        system_parts = []
        conversation = []
        for message in mapped:
            if message["role"] == "system":
                content = message["content"]
                if isinstance(content, str):
                    system_parts.append({"type": "text", "text": content})
                else:
                    system_parts.extend(content)
            else:
                conversation.append(message)
        if system_parts:
            content = "\n\n".join(part["text"] for part in system_parts)
            conversation.insert(0, ChatCompletionSystemMessageParam(role="system", content=content))
        return conversation


def _create_configured_chat_agent(model: Model) -> Agent[ChatDeps, AgentOutput]:
    """Создаёт ML-агента и регистрирует ту же журналируемую модель для Temporal."""

    journalled_model = journalled(model)
    return create_chat_agent(
        journalled_model,
        temporal_models={"backend-chat-model": model},
    )


if settings.validate_chat_worker_config():
    model_type = QwenChatModel if "qwen" in settings.chat_model.casefold() else OpenAIChatModel
    model = model_type(
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
