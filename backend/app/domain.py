"""Контракт снимка версии. Предметные атрибуты пока предварительные."""

import json
from datetime import datetime
from typing import Any, Literal, Self
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, JsonValue, model_validator


class Contract(BaseModel):
    model_config = ConfigDict(extra="forbid")

    @model_validator(mode="before")
    @classmethod
    def strings_are_storable(cls, value: Any) -> Any:
        # PostgreSQL не сохраняет NUL в тексте и JSONB, в том числе в ключах JSON.
        pending = [value]
        while pending:
            item = pending.pop()
            if isinstance(item, str):
                if "\x00" in item:
                    raise ValueError("Strings must not contain NUL (U+0000)")
            elif isinstance(item, dict):
                pending.extend(item.keys())
                pending.extend(item.values())
            elif isinstance(item, (list, tuple)):
                pending.extend(item)
        return value


class Entity(Contract):
    id: UUID
    kind: Literal[
        "discipline",
        "semester_load",
        "competency",
        "indicator",
        "learning_outcome",
        "control_form",
        "assessment_tool",
        "program",
        "curriculum_item",
        "course_topic",
        "score_scale",
    ]
    attributes: dict[str, JsonValue] = Field(default_factory=dict)
    source_ref_ids: list[UUID] = Field(default_factory=list)


class Relation(Contract):
    id: UUID
    source_id: UUID
    target_id: UUID
    type: str = Field(min_length=1)
    source_ref_ids: list[UUID] = Field(default_factory=list)
    confidence: float | None = Field(default=None, ge=0, le=1, allow_inf_nan=False)


class SnapshotSourceRef(Contract):
    id: UUID
    document_id: UUID
    section: str | None = None
    locator: str = Field(min_length=1)


class Snapshot(Contract):
    schema_version: Literal[1] = 1
    metadata: dict[str, JsonValue] = Field(default_factory=dict)
    entities: list[Entity] = Field(default_factory=list)
    relations: list[Relation] = Field(default_factory=list)
    source_refs: list[SnapshotSourceRef] = Field(default_factory=list)
    document_ids: list[UUID] = Field(default_factory=list)

    @model_validator(mode="after")
    def references_are_consistent(self) -> Self:
        for items in (self.entities, self.relations, self.source_refs):
            ids = [item.id for item in items]
            if len(ids) != len(set(ids)):
                raise ValueError("Duplicate IDs in snapshot")
        if len(self.document_ids) != len(set(self.document_ids)):
            raise ValueError("Duplicate document IDs")
        entities = {item.id for item in self.entities}
        refs = {item.id for item in self.source_refs}
        documents = set(self.document_ids)
        for relation in self.relations:
            if relation.source_id not in entities or relation.target_id not in entities:
                raise ValueError("Relation references an absent entity")
        for item in [*self.entities, *self.relations]:
            if not set(item.source_ref_ids).issubset(refs):
                raise ValueError("Evidence references an absent SourceRef")
        if any(ref.document_id not in documents for ref in self.source_refs):
            raise ValueError("SourceRef document is not included in this version")
        json.dumps(self.model_dump(mode="json"), allow_nan=False)
        return self


class ProgramCreate(Contract):
    name: str = Field(min_length=1, max_length=500)

    @model_validator(mode="after")
    def nonempty_name(self) -> Self:
        self.name = self.name.strip()
        if not self.name:
            raise ValueError("Program name cannot be blank")
        return self


class VersionCreate(Contract):
    expected_version_id: UUID
    snapshot: Snapshot


class VersionRestore(Contract):
    expected_version_id: UUID


class ProgramRead(Contract):
    id: UUID
    name: str
    current_version_id: UUID
    created_at: datetime


class VersionRead(Contract):
    id: UUID
    program_id: UUID
    number: int
    parent_version_id: UUID | None
    restored_from_version_id: UUID | None
    origin: Literal["initial", "save", "restore"]
    created_at: datetime
    snapshot: Snapshot


class DocumentRead(Contract):
    id: UUID
    program_id: UUID
    original_name: str
    format: Literal["pdf", "docx", "xlsx"]
    media_type: str
    size_bytes: int
    sha256: str
    created_at: datetime
    parsing_status: Literal["not_parsed"] = "not_parsed"
