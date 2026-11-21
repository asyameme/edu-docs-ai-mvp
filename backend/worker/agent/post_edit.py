"""Применение предложения и проверка норм после правки — чистые функции.

Конвейер поверх ``SnapshotV2`` без базы и Temporal: проверка предложения
(``proposal_apply``), применение правок по одной, после КАЖДОЙ правки — прогон
активных правил на документных проекциях новой версии (``graph_projections``) и
вход генерации для точечных DOCX-замен. Создание версий, транзакции и запись в
``validation_issues`` — за бекендом (``write_port.ChatWritePort``).

R-07 и R-08 детерминированно готовят варианты для решения пользователя.
``include_semantic=False`` отключает только проверки, которым действительно нужна LLM.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal
from uuid import UUID

from app.domain import SnapshotV2
from worker.agent.contracts import ChangeProposal, ValidationFinding
from worker.agent.graph_projections import document_projections
from worker.agent.proposal_apply import (
    ApprovedChange,
    ProposalApplicationError,
    apply_change,
    generation_inputs,
    plan_approved_changes,
    touched_document_ids,
)
from worker.agent.validation_adapter import findings_to_issues
from worker.generation.patches import FieldChange, FieldEvidence, SourceEvidence
from worker.validation.semantic_llm import SemanticEvaluator
from worker.validation.validator import load_rules, run_validation

_SEMANTIC_RULE_TYPES = {
    "semantic_rpd_alignment",
    "competency_coverage",
}


@dataclass(frozen=True, slots=True)
class AppliedEdit:
    """Одна применённая правка и находки проверки норм сразу после неё."""

    change: ApprovedChange
    findings: tuple[ValidationFinding, ...]


@dataclass(frozen=True, slots=True)
class PostEditOutcome:
    """Итог применения предложения: все правки или обоснованный отказ."""

    status: Literal["applied", "rejected"]
    applied: tuple[AppliedEdit, ...] = ()
    final_snapshot: SnapshotV2 | None = None
    rejection_reason: str | None = None
    generation: tuple[list[FieldChange], list[FieldEvidence], list[SourceEvidence]] | None = None
    touched_document_ids: frozenset[UUID] = frozenset()


def _rules_for_run(
    rules_config: dict | None, include_semantic: bool
) -> dict:
    config = rules_config if rules_config is not None else load_rules()
    if include_semantic:
        return config
    return {
        **config,
        "rules": [
            rule
            for rule in config.get("rules", [])
            if rule.get("type") not in _SEMANTIC_RULE_TYPES
        ],
    }


def validate_snapshot(
    snapshot: SnapshotV2,
    *,
    rules_config: dict | None = None,
    semantic_evaluator: SemanticEvaluator | None = None,
    include_semantic: bool = True,
) -> list[ValidationFinding]:
    """Прогон активных правил фазы graph_validation на проекциях версии."""

    config = _rules_for_run(rules_config, include_semantic)
    report = run_validation(
        document_projections(snapshot),
        config,
        phase="graph_validation",
        semantic_evaluator=semantic_evaluator,
    )
    return [
        ValidationFinding(
            id=UUID(issue["id"]),
            rule_code=issue["rule_code"],
            type=issue["type"],
            severity=issue["severity"],
            status=issue["status"],
            details=issue["details"],
        )
        for issue in findings_to_issues(report, config)
    ]


def apply_proposal_in_memory(
    snapshot: SnapshotV2,
    proposal: ChangeProposal,
    *,
    rules_config: dict | None = None,
    semantic_evaluator: SemanticEvaluator | None = None,
    include_semantic: bool = True,
) -> PostEditOutcome:
    """Применить предложение к снимку и проверить нормы после каждой правки.

    Отказ возможен только до первой правки: ``plan_approved_changes``
    проверяет предложение целиком, частично применённых состояний не бывает.
    """

    try:
        approved = plan_approved_changes(snapshot, proposal)
    except ProposalApplicationError as error:
        return PostEditOutcome(status="rejected", rejection_reason=str(error))
    current = snapshot
    applied: list[AppliedEdit] = []
    for change in approved:
        current = apply_change(current, change)
        applied.append(
            AppliedEdit(
                change,
                tuple(
                    validate_snapshot(
                        current,
                        rules_config=rules_config,
                        semantic_evaluator=semantic_evaluator,
                        include_semantic=include_semantic,
                    )
                ),
            )
        )
    return PostEditOutcome(
        status="applied",
        applied=tuple(applied),
        final_snapshot=current,
        generation=generation_inputs(current, approved),
        touched_document_ids=frozenset(touched_document_ids(snapshot, approved)),
    )
