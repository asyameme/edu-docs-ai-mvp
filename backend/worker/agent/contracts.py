"""Typed inputs and outputs shared by the chat agent and its backend adapter."""

from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, JsonValue

from app.domain import Entity, RelationV2, SourceRefV2


class ChatDeps(BaseModel):
    """Компактный контекст, передаваемый Pydantic AI через Temporal."""

    model_config = ConfigDict(extra="forbid")

    program_id: UUID
    version_id: UUID


class EntitySearchHit(BaseModel):
    model_config = ConfigDict(extra="forbid")

    entity: Entity
    summary: str
    truncated: bool = False
    source_refs: list[SourceRefV2] = Field(default_factory=list)


class EntitySearchResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    hits: list[EntitySearchHit] = Field(default_factory=list)


class EntityReadResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    entity: Entity
    matched_context: str | None = None
    truncated: bool = False
    relations: list[RelationV2] = Field(default_factory=list)
    source_refs: list[SourceRefV2] = Field(default_factory=list)


class SourceFragment(BaseModel):
    model_config = ConfigDict(extra="forbid")

    source_ref: SourceRefV2
    document_name: str
    text: str
    surrounding_text: str = ""


class GraphProjection(BaseModel):
    """Ограниченная проекция графа вокруг сущности."""

    model_config = ConfigDict(extra="forbid")

    entities: list[Entity] = Field(default_factory=list)
    relations: list[RelationV2] = Field(default_factory=list)
    source_refs: list[SourceRefV2] = Field(default_factory=list)


class ValidationFinding(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: UUID
    rule_code: str
    type: str
    severity: Literal["warning", "blocking"]
    status: Literal["open", "needs_review"]
    details: dict[str, JsonValue]


class Answer(BaseModel):
    model_config = ConfigDict(extra="forbid")

    kind: Literal["answer"]
    message: str = Field(min_length=1)
    sources: list[SourceRefV2] = Field(default_factory=list)


class Clarification(BaseModel):
    model_config = ConfigDict(extra="forbid")

    kind: Literal["clarification"]
    message: str = Field(min_length=1)
    questions: list[str] = Field(min_length=1)


class ProposedAttributeChange(BaseModel):
    """A suggestion only. The backend must validate it before applying it."""

    model_config = ConfigDict(extra="forbid")

    target_kind: Literal["entity", "relation"]
    target_id: UUID
    field_path: str = Field(min_length=1)
    expected_old_value: JsonValue
    new_value: JsonValue
    reason: str = Field(min_length=1)
    source_ref_ids: list[UUID] = Field(min_length=1)


class ChangeProposal(BaseModel):
    model_config = ConfigDict(extra="forbid")

    kind: Literal["change_proposal"]
    summary: str = Field(min_length=1)
    changes: list[ProposedAttributeChange] = Field(min_length=1)
    unresolved_questions: list[str] = Field(default_factory=list)


ChatOutput = Answer | Clarification | ChangeProposal
