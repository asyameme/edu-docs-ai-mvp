"""Контракт снимка версии. Предметные атрибуты пока предварительные."""

import json
import re
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
        "course_content",
        "score_scale",
        "document_section",
        "practice_stage",
        "professional_role",
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

    @model_validator(mode="after")
    def nonempty_type(self) -> Self:
        if not self.type.strip():
            raise ValueError("Relation type cannot be blank")
        return self


class SnapshotSourceRef(Contract):
    id: UUID
    document_id: UUID
    section: str | None = None
    locator: str = Field(min_length=1)


class SnapshotData(Contract):
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


class VersionRestore(Contract):
    expected_version_id: UUID


class RelationV2(Relation):
    attributes: dict[str, JsonValue] = Field(default_factory=dict)


class SourceRefV2(SnapshotSourceRef):
    artifact_id: UUID | None = None
    artifact_locator: str | None = Field(default=None, min_length=1)

    @model_validator(mode="after")
    def artifact_has_location(self) -> Self:
        if (self.artifact_id is None) != (self.artifact_locator is None):
            raise ValueError("Artifact and its location must be specified together")
        if not self.locator.strip():
            raise ValueError("Original location cannot be blank")
        if self.artifact_locator is not None and not self.artifact_locator.strip():
            raise ValueError("Artifact location cannot be blank")
        return self


class SourceLink(Contract):
    id: UUID
    target_kind: Literal["entity", "relation"]
    target_id: UUID
    field_path: str = Field(min_length=1)
    source_ref_id: UUID
    observed_value: dict[str, JsonValue]


class ReviewDecision(Contract):
    conflict_id: UUID
    variant_id: UUID
    check_job_id: UUID
    changes: list[dict[str, JsonValue]]
    decided_at: datetime
    author: str | None = None


class ReviewConfirmation(Contract):
    checked_version_id: UUID
    check_job_id: UUID
    content_hash: str = Field(pattern=r"^[a-f0-9]{64}$")
    accepted_warning_ids: list[UUID]
    confirmed_at: datetime
    author: str | None = None


class ReviewState(Contract):
    review_id: UUID
    import_job_id: UUID
    analysis_version_id: UUID
    decisions: list[ReviewDecision] = Field(default_factory=list)
    confirmation: ReviewConfirmation | None = None


class SnapshotV2(SnapshotData):
    schema_version: Literal[2] = 2
    relations: list[RelationV2] = Field(default_factory=list)
    source_refs: list[SourceRefV2] = Field(default_factory=list)
    source_links: list[SourceLink] = Field(default_factory=list)
    review: ReviewState | None = None

    @model_validator(mode="after")
    def field_evidence_is_consistent(self) -> Self:
        targets = {
            **{("entity", item.id): item for item in self.entities},
            **{("relation", item.id): item for item in self.relations},
        }
        refs = {item.id for item in self.source_refs}
        identities, locations = set(), set()
        for link in self.source_links:
            key = (link.target_kind, link.target_id, link.field_path, link.source_ref_id)
            if link.id in identities or key in locations:
                raise ValueError("Duplicate field evidence")
            identities.add(link.id)
            locations.add(key)
            target = targets.get((link.target_kind, link.target_id))
            if target is None or link.source_ref_id not in refs:
                raise ValueError("Field evidence references an absent object or source")
            if "value" not in link.observed_value:
                raise ValueError("Observed value must contain value, including explicit null")
            unit = link.observed_value.get("unit")
            if unit is not None and (not isinstance(unit, str) or not unit.strip()):
                raise ValueError("Observation unit must be a nonempty string")
            if link.field_path != "$":
                if not link.field_path.startswith("/") or re.search(r"~(?![01])", link.field_path):
                    raise ValueError("Field location must be a JSON Pointer or $")
                value = target.model_dump(mode="json")
                for part in link.field_path[1:].split("/"):
                    part = part.replace("~1", "/").replace("~0", "~")
                    try:
                        if isinstance(value, list):
                            if not part.isascii() or not part.isdigit():
                                raise ValueError("Invalid array index")
                            value = value[int(part)]
                        else:
                            value = value[part]
                    except (KeyError, IndexError, TypeError) as error:
                        raise ValueError("Field evidence points to an absent field") from error
        for relation in self.relations:
            if "matrix_value" in relation.attributes:
                matrix = relation.attributes["matrix_value"]
                if matrix is not None and (
                    type(matrix) is not int or matrix not in (0, 1)
                    or relation.type != "FORMS_COMPETENCY"
                ):
                    raise ValueError("Matrix value must be integer 0 or 1")
        if self.review:
            conflicts = [item.conflict_id for item in self.review.decisions]
            if len(conflicts) != len(set(conflicts)):
                raise ValueError("More than one current decision for a conflict")
        return self


class VersionCreate(Contract):
    expected_version_id: UUID
    snapshot: SnapshotV2

    @model_validator(mode="after")
    def review_is_server_owned(self) -> Self:
        if self.snapshot.review is not None:
            raise ValueError(
                "Состояние проверки устанавливается только через сценарий подтверждения"
            )
        return self


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
    snapshot: SnapshotV2


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
