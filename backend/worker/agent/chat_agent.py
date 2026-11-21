"""Pydantic AI behavior and read-only tools for the program chat."""

from collections.abc import Mapping
from typing import Annotated
from uuid import UUID

from pydantic import Field
from pydantic_ai import Agent, RunContext
from pydantic_ai.durable_exec.temporal import TemporalDurability
from pydantic_ai.models import Model
from pydantic_ai.models.openai import OpenAIChatModelSettings
from temporalio.common import RetryPolicy

from .contracts import (
    ChatDeps,
    ChatOutput,
    EntityReadResult,
    EntitySearchResult,
    GraphProjection,
    SourceFragment,
    ValidationFinding,
)
from .llm_journal import journalled
from .read_port import EntityKind, get_worker_read_port

# Qwen на vLLM по умолчанию размышляет: тратит лимит токенов на reasoning и
# возвращает пустой ответ. Тот же костыль, что в validation/semantic_llm.py.
MODEL_SETTINGS = OpenAIChatModelSettings(
    extra_body={"chat_template_kwargs": {"enable_thinking": False}}
)

INSTRUCTIONS = """
Ты чат-агент системы поддержки документации образовательной программы.

- Отвечай кратко и по-русски; перечисляй только факты, проверенные инструментами.
- Для утверждений о состоянии программы используй результаты инструментов.
- Не придумывай значения, связи, формулировки или ссылки на источники.
- Если сущность или данные не нашлись — скажи об этом прямо, без догадок.
- Если данные сокращены (truncated=true), используй summary/matched_context;
  для нужного фрагмента передай в read_entity query с искомой формулировкой.
- Если запрос неоднозначен, верни Clarification с конкретными вопросами.
- Если пользователь просит изменить данные, сначала покажи текущее состояние
  сущности (read_entity или read_graph_projection) и только затем верни
  ChangeProposal с текущими и предлагаемыми значениями и источниками.
- Предлагая правку того же поля повторно, сначала перечитай сущность:
  значение могло измениться с прошлой правки.
- На вопросы о нарушениях и замечаниях, а также после предложения правок
  используй get_validation_findings.
- ChangeProposal является только предложением. Не утверждай, что данные изменены.
- Не выбирай канонический код или формулировку при расхождении источников.
  Покажи варианты и попроси пользователя выбрать.
""".strip()


def create_chat_agent(
    model: Model,
    *,
    temporal_models: Mapping[str, Model] | None = None,
) -> Agent[ChatDeps, ChatOutput]:
    """Создаёт агента с необязательными алиасами моделей для backend Temporal."""

    agent = Agent(
        journalled(model),
        name="edu_program_chat_v1",
        deps_type=ChatDeps,
        output_type=ChatOutput,
        instructions=INSTRUCTIONS,
        capabilities=[
            TemporalDurability(
                models=temporal_models,
                # Не даём сетевой ошибке модели бесконечно удерживать ход до лимита workflow.
                model_activity_config={"retry_policy": RetryPolicy(maximum_attempts=3)},
            )
        ],
        model_settings=MODEL_SETTINGS,
        retries=2,
    )

    @agent.tool
    async def search_entities(
        ctx: RunContext[ChatDeps],
        query: Annotated[str, Field(min_length=1, max_length=500)],
        entity_kinds: list[EntityKind] | None = None,
        limit: Annotated[int, Field(ge=1, le=20)] = 10,
    ) -> EntitySearchResult:
        """Находит сущности по имени или содержимому в выбранной версии программы."""

        return await get_worker_read_port().search_entities(
            program_id=ctx.deps.program_id,
            version_id=ctx.deps.version_id,
            query=query,
            entity_kinds=entity_kinds,
            limit=limit,
        )

    @agent.tool
    async def read_entity(
        ctx: RunContext[ChatDeps],
        entity_id: UUID,
        query: Annotated[str | None, Field(min_length=1, max_length=500)] = None,
    ) -> EntityReadResult:
        """Читает сущность, её прямые связи и ссылки на источники."""

        return await get_worker_read_port().read_entity(
            program_id=ctx.deps.program_id,
            version_id=ctx.deps.version_id,
            entity_id=entity_id,
            query=query,
        )

    @agent.tool
    async def read_source_fragment(
        ctx: RunContext[ChatDeps],
        source_ref_id: UUID,
        context_paragraphs: Annotated[int, Field(ge=0, le=2)] = 1,
    ) -> SourceFragment:
        """Читает фрагмент источника и небольшой окружающий контекст."""

        return await get_worker_read_port().read_source_fragment(
            program_id=ctx.deps.program_id,
            version_id=ctx.deps.version_id,
            source_ref_id=source_ref_id,
            context_paragraphs=context_paragraphs,
        )

    @agent.tool
    async def read_graph_projection(
        ctx: RunContext[ChatDeps],
        entity_id: UUID,
        depth: Annotated[int, Field(ge=0, le=2)] = 1,
    ) -> GraphProjection:
        """Читает ограниченную часть графа вокруг сущности."""

        return await get_worker_read_port().read_graph_projection(
            program_id=ctx.deps.program_id,
            version_id=ctx.deps.version_id,
            entity_id=entity_id,
            depth=depth,
        )

    @agent.tool
    async def get_validation_findings(
        ctx: RunContext[ChatDeps],
        limit: Annotated[int, Field(ge=1, le=50)] = 20,
    ) -> list[ValidationFinding]:
        """Читает сохранённые замечания проверки выбранной версии."""

        return await get_worker_read_port().get_validation_findings(
            program_id=ctx.deps.program_id, version_id=ctx.deps.version_id, limit=limit
        )

    return agent
