"""Предложение обмена Backend/ML: анализ пакета и проверка выбранного снимка."""

import re
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


class DocumentMutation(Contract):
    """A selected field value in one parsed document, before graph assembly."""

    op: Literal["set_document_value"]
    document_id: UUID
    field_path: str = Field(min_length=1)
    expected_old_value: JsonValue
    value: JsonValue = None

    @model_validator(mode="after")
    def document_field_is_explicit(self) -> Self:
        required = {"expected_old_value", "value"}
        if not required.issubset(self.model_fields_set):
            raise ValueError("Expected and selected values must be supplied, including null")
        if not self.field_path.startswith("/") or re.search(r"~(?![01])", self.field_path):
            raise ValueError("Document field must use a valid JSON Pointer")
        parts = [
            part.replace("~1", "/").replace("~0", "~")
            for part in self.field_path[1:].split("/")
        ]
        if len(parts) < 2 or any(not part for part in parts):
            raise ValueError("Document field path must point to a parsed document value")
        if "meta" in parts or "source_ref" in parts or parts[0] == "parse_issues":
            raise ValueError(
                "Document identity, provenance, and parser diagnostics cannot be changed"
            )
        return self


class CompetencyLinksMutation(Contract):
    """Replace the competency set of one identified discipline or plan row."""

    op: Literal["set_competency_links"]
    document_id: UUID
    collection: Literal["disciplines", "curriculum_items"]
    record_index: int = Field(ge=0)
    expected_codes: list[str]
    codes: list[str] = Field(min_length=1)


ReviewMutation = Mutation | DocumentMutation | CompetencyLinksMutation


class Variant(Contract):
    id: UUID
    label: str = Field(min_length=1)
    changes: list[ReviewMutation] = Field(min_length=1)


class RpdAlignmentEvidenceItem(Contract):
    id: str | None = None
    topic: str | None = None
    activity: str | None = None
    indicator_code: str | None = None
    indicator_text: str | None = None
    assessment_tool: str | None = None


class RpdAlignmentSummary(Contract):
    outcome_id: str | None = None
    outcome_text: str | None = None
    classification: Literal["supported", "gap", "uncertain", "invalid"] | None = None
    reason: str | None = None
    # Детали свидетельств, на которые ссылается пояснение модели:
    # темы раздела 2 и цепочка индикатор → оценочное средство.
    course_evidence: list[RpdAlignmentEvidenceItem] | None = None
    assessment_evidence: list[RpdAlignmentEvidenceItem] | None = None


class ReviewIssue(Contract):
    id: UUID
    rule_code: str = Field(min_length=1)
    rule_revision: str = Field(min_length=1)
    type: str = Field(min_length=1)
    severity: Literal["warning", "blocking"]
    status: Literal["open", "needs_review"] = "open"
    message: str = Field(min_length=1)
    source_ref_ids: list[UUID] = Field(default_factory=list)
    source_document_ids: list[UUID] = Field(default_factory=list)
    requires_decision: bool = False
    variants: list[Variant] = Field(default_factory=list)
    rpd_alignment: RpdAlignmentSummary | None = None

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
    required_rules: list[str] = Field(default_factory=list)
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
    status: Literal["passed", "failed", "needs_review", "skipped", "not_applicable"]
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


class AnalysisV2(CheckResult):
    """Document-level reconciliation result; it must not contain a graph."""

    schema_version: Literal[2] = 2
    artifacts: list[MarkdownArtifact] = Field(default_factory=list)

    @model_validator(mode="after")
    def issue_variants_are_document_changes(self) -> Self:
        ids = [item.id for item in self.artifacts]
        documents = [item.document_id for item in self.artifacts]
        if len(ids) != len(set(ids)) or len(documents) != len(set(documents)):
            raise ValueError("One Markdown artifact per document and analysis is allowed")
        for issue in self.issues:
            if issue.requires_decision and not issue.variants:
                raise ValueError("A decision issue must provide at least one variant")
            if issue.variants and any(
                not isinstance(change, (DocumentMutation, CompetencyLinksMutation))
                for variant in issue.variants
                for change in variant.changes
            ):
                raise ValueError("Document reconciliation variants must target parsed JSON fields")
        return self


class ReviewCreate(Contract):
    import_job_id: UUID
    expected_version_id: UUID
    rerun_same_import: bool = False


class DecisionCreate(Contract):
    expected_version_id: UUID
    conflict_id: UUID
    variant_id: UUID
    signature: str = Field(min_length=1, max_length=120)
    check_job_id: UUID | None = None

    @field_validator("signature")
    @classmethod
    def normalize_signature(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("Подпись не должна быть пустой")
        return value


class DecisionSelection(Contract):
    conflict_id: UUID
    variant_id: UUID


class DecisionBatchCreate(Contract):
    expected_version_id: UUID
    selections: list[DecisionSelection] = Field(min_length=1, max_length=500)
    signature: str = Field(min_length=1, max_length=120)

    @field_validator("signature")
    @classmethod
    def normalize_signature(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("Подпись не должна быть пустой")
        return value

    @model_validator(mode="after")
    def conflict_ids_are_unique(self) -> Self:
        ids = [item.conflict_id for item in self.selections]
        if len(ids) != len(set(ids)):
            raise ValueError("Расхождения в пакетном выборе не должны повторяться")
        return self


class CheckCreate(Contract):
    version_id: UUID
    rule_codes: list[str] | None = Field(default=None, min_length=1, max_length=2)
    # Ручная проверка R-11 может запускаться по одному документу РПД,
    # чтобы не ждать LLM-оценку всех дисциплин пакета.
    document_ids: list[UUID] | None = Field(default=None, min_length=1, max_length=4)

    @field_validator("rule_codes")
    @classmethod
    def selected_rules_are_unique(cls, value: list[str] | None) -> list[str] | None:
        if value is not None and len(value) != len(set(value)):
            raise ValueError("Ручные проверки не должны повторяться")
        return value

    @model_validator(mode="after")
    def documents_only_for_manual_rules(self) -> Self:
        if self.document_ids is not None and self.rule_codes is None:
            raise ValueError("Список документов допустим только для ручных правил")
        if self.document_ids is not None and len(self.document_ids) != len(set(self.document_ids)):
            raise ValueError("Документы ручной проверки не должны повторяться")
        return self


class ConfirmCreate(Contract):
    expected_version_id: UUID
    check_job_id: UUID
    accepted_warning_ids: list[UUID] = Field(default_factory=list)
    signature: str = Field(min_length=1, max_length=120)

    @field_validator("signature")
    @classmethod
    def normalize_signature(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("Подпись не должна быть пустой")
        return value


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


class PrepareRequestV2(Contract):
    schema_version: Literal[2] = 2
    operation: Literal["prepare"] = "prepare"
    job_id: UUID
    program_id: UUID
    version_id: UUID
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
    rule_codes: list[str] | None = Field(default=None, min_length=1, max_length=2)
    document_ids: list[UUID] | None = Field(default=None, min_length=1, max_length=4)

    @model_validator(mode="after")
    def documents_belong_to_program(self) -> Self:
        ids = [item.document_id for item in self.documents]
        if len(ids) != len(set(ids)):
            raise ValueError("Документы проверки не должны повторяться")
        for document in self.documents:
            document.check_program(self.program_id)
        if self.rule_codes is not None and len(self.rule_codes) != len(set(self.rule_codes)):
            raise ValueError("Ручные проверки не должны повторяться")
        return self
