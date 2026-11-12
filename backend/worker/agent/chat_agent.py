"""Pydantic AI behavior and read-only tools for the program chat."""

from typing import Annotated
from uuid import UUID

from pydantic import Field
from pydantic_ai import Agent, RunContext
from pydantic_ai.durable_exec.temporal import TemporalDurability
from pydantic_ai.models import Model

from .contracts import (
    ChatDeps,
    ChatOutput,
    EntityReadResult,
    EntitySearchResult,
    GraphProjection,
    SourceFragment,
)
from .read_port import EntityKind, get_worker_read_port


INSTRUCTIONS = """
Ты чат-агент системы поддержки документации образовательной программы.

- Для утверждений о состоянии программы используй результаты инструментов.
- Не придумывай значения, связи, формулировки или ссылки на источники.
- Если запрос неоднозначен, верни Clarification с конкретными вопросами.
- Если пользователь просит изменить данные, сначала прочитай текущую сущность
  и верни ChangeProposal с текущими и предлагаемыми значениями и источниками.
- ChangeProposal является только предложением. Не утверждай, что данные изменены.
- Не выбирай канонический код или формулировку при расхождении источников.
  Покажи варианты и попроси пользователя выбрать.
""".strip()


def create_chat_agent(model: Model) -> Agent[ChatDeps, ChatOutput]:
    """Build a stable agent definition; backend supplies the configured model."""

    agent = Agent(
        model,
        name="edu_program_chat_v1",
        deps_type=ChatDeps,
        output_type=ChatOutput,
        instructions=INSTRUCTIONS,
        capabilities=[TemporalDurability()],
    )

    @agent.tool
    async def search_entities(
        ctx: RunContext[ChatDeps],
        query: Annotated[str, Field(min_length=1, max_length=500)],
        entity_kinds: list[EntityKind] | None = None,
        limit: Annotated[int, Field(ge=1, le=20)] = 10,
    ) -> EntitySearchResult:
        """Find entities in the selected program version by name or content."""

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
    ) -> EntityReadResult:
        """Read one entity, its direct relations, and its source references."""

        return await get_worker_read_port().read_entity(
            program_id=ctx.deps.program_id,
            version_id=ctx.deps.version_id,
            entity_id=entity_id,
        )

    @agent.tool
    async def read_source_fragment(
        ctx: RunContext[ChatDeps],
        source_ref_id: UUID,
        context_paragraphs: Annotated[int, Field(ge=0, le=2)] = 1,
    ) -> SourceFragment:
        """Read a source fragment and a small amount of surrounding context."""

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
        """Read a bounded part of the graph around an entity."""

        return await get_worker_read_port().read_graph_projection(
            program_id=ctx.deps.program_id,
            version_id=ctx.deps.version_id,
            entity_id=entity_id,
            depth=depth,
        )

    return agent
