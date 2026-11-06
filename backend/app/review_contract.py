"""Предложение обмена Backend/ML: анализ пакета и проверка выбранного снимка."""

from typing import Literal, Self
from uuid import UUID

from pydantic import AnyHttpUrl, Field, JsonValue, field_validator, model_validator

from app.domain import Contract, Entity, RelationV2, SnapshotV2
from app.parser_document import ParserDocument, parse_output


class Mutation(Contract):
    op: Literal[
        "set_attribute",
        "add_entity",
        "remove_entity",
        "add_relation",
        "update_relation",
        "remove_relation",
    ]
    target_kind: Literal["entity", "relation"]
    target_id: UUID
    field_path: str | None = None
    value: JsonValue = None
    entity: Entity | None = None
    relation: RelationV2 | None = None

    @model_validator(mode="after")
    def operation_is_typed(self) -> Self:
        if self.op != "set_attribute" and (self.field_path is not None or self.value is not None):
            raise ValueError("Object operations cannot contain a field assignment")
        if self.entity is not None and self.op != "add_entity":
            raise ValueError("Entity payload is only allowed for add_entity")
        if self.relation is not None and self.op not in {"add_relation", "update_relation"}:
            raise ValueError("Relation payload is only allowed for relation writes")
        if self.op == "set_attribute":
            if "value" not in self.model_fields_set:
                raise ValueError("Field value must be supplied, including explicit null")
            if not self.field_path or not self.field_path.startswith("/attributes/"):
                raise ValueError("Only attributes can be changed by a field operation")
        elif self.op == "add_entity":
            if (
                self.target_kind != "entity"
                or self.entity is None
                or self.entity.id != self.target_id
            ):
                raise ValueError("New entity and its ID are required")
        elif self.op in {"add_relation", "update_relation"}:
            if (
                self.target_kind != "relation"
                or self.relation is None
                or self.relation.id != self.target_id
            ):
                raise ValueError("Relation and its ID are required")
        elif self.op == "remove_entity" and self.target_kind != "entity":
            raise ValueError("Entity removal requires an entity target")
        elif self.op == "remove_relation" and self.target_kind != "relation":
            raise ValueError("Relation removal requires a relation target")
        return self


class Variant(Contract):
    id: UUID
    label: str = Field(min_length=1)
    changes: list[Mutation] = Field(min_length=1)


class ReviewIssue(Contract):
    id: UUID
    rule_code: str = Field(min_length=1)
    rule_revision: str = Field(min_length=1)
    type: str = Field(min_length=1)
    severity: Literal["warning", "blocking"]
    status: Literal["open", "needs_review"] = "open"
    message: str = Field(min_length=1)
    source_ref_ids: list[UUID] = Field(default_factory=list)
    requires_decision: bool = False
    variants: list[Variant] = Field(default_factory=list)

    @model_validator(mode="after")
    def variants_are_distinct(self) -> Self:
        ids = [item.id for item in self.variants]
        if len(ids) != len(set(ids)):
            raise ValueError("Duplicate variants")
        if self.variants and not self.requires_decision:
            raise ValueError("Variants require an explicit user decision")
        return self


class Ruleset(Contract):
    id: str = Field(min_length=1)
    revision: str = Field(min_length=1)
    config: dict[str, JsonValue]
    required_rules: list[str] = Field(min_length=1)
    required_document_types: list[str] = Field(min_length=1)

    @model_validator(mode="after")
    def requirements_are_distinct(self) -> Self:
        for values in (self.required_rules, self.required_document_types):
            if len(values) != len(set(values)) or any(not value.strip() for value in values):
                raise ValueError("Requirements must be nonempty and unique")
        return self


class RuleCoverage(Contract):
    rule_code: str = Field(min_length=1)
    rule_revision: str = Field(min_length=1)
    status: Literal["passed", "failed", "skipped", "not_applicable"]
    reason: str | None = None


class CheckResult(Contract):
    schema_version: Literal[1] = 1
    ruleset: Ruleset
    coverage: list[RuleCoverage]
    issues: list[ReviewIssue] = Field(default_factory=list)

    @model_validator(mode="after")
    def result_is_complete(self) -> Self:
        codes = [item.rule_code for item in self.coverage]
        ids = [item.id for item in self.issues]
        if len(codes) != len(set(codes)) or len(ids) != len(set(ids)):
            raise ValueError("Duplicate rule results or issues")
        coverage = {item.rule_code: item for item in self.coverage}
        if not set(self.ruleset.required_rules).issubset(coverage):
            raise ValueError("Required rule coverage is missing")
        for issue in self.issues:
            result = coverage.get(issue.rule_code)
            if (
                result is None
                or result.rule_revision != issue.rule_revision
                or result.status != "failed"
            ):
                raise ValueError("Issue contradicts rule coverage")
        if any(
            item.status == "failed" and not any(i.rule_code == item.rule_code for i in self.issues)
            for item in self.coverage
        ):
            raise ValueError("Failed rule requires an explanation")
        return self


class MarkdownArtifact(Contract):
    id: UUID
    document_id: UUID
    parser_revision: str = Field(min_length=1)
    parser_config_hash: str = Field(pattern=r"^[a-f0-9]{64}$")
    parser_config: dict[str, JsonValue]
    markdown: str


class AnalysisV1(CheckResult):
    candidate: SnapshotV2
    artifacts: list[MarkdownArtifact] = Field(default_factory=list)

    @model_validator(mode="after")
    def analysis_is_unconfirmed(self) -> Self:
        if self.candidate.review is not None:
            raise ValueError("ML cannot assign review decisions or confirmation")
        if not self.candidate.entities:
            raise ValueError("Analysis did not produce program data")
        if any(
            not item.source_ref_ids
            for item in [*self.candidate.entities, *self.candidate.relations]
        ):
            raise ValueError("Extracted entities and relations require original evidence")
        refs = {item.id for item in self.candidate.source_refs}
        for issue in self.issues:
            if not set(issue.source_ref_ids).issubset(refs):
                raise ValueError("Issue references an absent source")
        ids = [item.id for item in self.artifacts]
        documents = [item.document_id for item in self.artifacts]
        if len(ids) != len(set(ids)) or len(documents) != len(set(documents)):
            raise ValueError("One Markdown artifact per document and analysis is allowed")
        if not set(documents).issubset(self.candidate.document_ids):
            raise ValueError("Artifact document is absent from the candidate")
        return self


class ReviewCreate(Contract):
    import_job_id: UUID
    expected_version_id: UUID


class DecisionCreate(Contract):
    expected_version_id: UUID
    conflict_id: UUID
    variant_id: UUID
    check_job_id: UUID | None = None


class CheckCreate(Contract):
    version_id: UUID


class ConfirmCreate(Contract):
    expected_version_id: UUID
    check_job_id: UUID
    accepted_warning_ids: list[UUID] = Field(default_factory=list)


class AnalysisDocument(Contract):
    document_id: UUID
    sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    parsed: ParserDocument
    original_url: AnyHttpUrl | None = None

    @field_validator("parsed", mode="before")
    @classmethod
    def read_document_version(cls, value):
        return parse_output(value)

    @model_validator(mode="after")
    def hash_matches_original(self) -> Self:
        if self.parsed.meta.file_hash != self.sha256:
            raise ValueError("Parser result and original have different hashes")
        return self

    def check_program(self, program_id: UUID) -> None:
        # В прежнем meta.program_id встречается UUID, в документах — академический код.
        try:
            legacy_id = UUID(self.parsed.meta.program_id or "")
        except ValueError:
            legacy_id = None
        if legacy_id is not None and legacy_id != program_id:
            raise ValueError("Результат документа принадлежит другой программе")


class PrepareRequest(Contract):
    schema_version: Literal[1] = 1
    operation: Literal["prepare"] = "prepare"
    job_id: UUID
    program_id: UUID
    version_id: UUID
    base_snapshot: SnapshotV2
    documents: list[AnalysisDocument] = Field(min_length=1)

    @model_validator(mode="after")
    def documents_are_unique(self) -> Self:
        ids = [item.document_id for item in self.documents]
        if len(ids) != len(set(ids)):
            raise ValueError("Duplicate input documents")
        for document in self.documents:
            document.check_program(self.program_id)
        return self


class CheckRequest(Contract):
    schema_version: Literal[1] = 1
    operation: Literal["check"] = "check"
    job_id: UUID
    program_id: UUID
    version_id: UUID
    snapshot: SnapshotV2
    ruleset: Ruleset
    documents: list[AnalysisDocument] = Field(default_factory=list)

    @model_validator(mode="after")
    def documents_belong_to_program(self) -> Self:
        ids = [item.document_id for item in self.documents]
        if len(ids) != len(set(ids)):
            raise ValueError("Документы проверки не должны повторяться")
        for document in self.documents:
            document.check_program(self.program_id)
        return self
