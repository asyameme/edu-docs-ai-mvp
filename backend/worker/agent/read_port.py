"""Read-only tool boundary implemented by the backend worker."""

from typing import Protocol
from uuid import UUID

from app.domain import EntityKind

from .contracts import (
    EntityReadResult,
    EntitySearchResult,
    GraphProjection,
    SourceFragment,
    ValidationFinding,
)


class ChatReadPort(Protocol):
    """Operations behind the agent tools; this protocol performs no writes."""

    async def search_entities(
        self,
        *,
        program_id: UUID,
        version_id: UUID,
        query: str,
        entity_kinds: list[EntityKind] | None,
        limit: int,
    ) -> EntitySearchResult: ...

    async def read_entity(
        self,
        *,
        program_id: UUID,
        version_id: UUID,
        entity_id: UUID,
        query: str | None = None,
    ) -> EntityReadResult: ...

    async def read_source_fragment(
        self,
        *,
        program_id: UUID,
        version_id: UUID,
        source_ref_id: UUID,
        context_paragraphs: int,
    ) -> SourceFragment: ...

    async def read_graph_projection(
        self,
        *,
        program_id: UUID,
        version_id: UUID,
        entity_id: UUID,
        depth: int,
    ) -> GraphProjection: ...

    async def get_validation_findings(
        self, *, program_id: UUID, version_id: UUID, limit: int
    ) -> list[ValidationFinding]: ...


_worker_read_port: ChatReadPort | None = None


def configure_worker_read_port(port: ChatReadPort) -> None:
    """Bind the worker's read adapter once during startup, before polling tasks."""

    global _worker_read_port
    if _worker_read_port is not None and _worker_read_port is not port:
        raise RuntimeError("Chat read port is already configured")
    _worker_read_port = port


def get_worker_read_port() -> ChatReadPort:
    if _worker_read_port is None:
        raise RuntimeError("Chat read port has not been configured by the worker")
    return _worker_read_port
