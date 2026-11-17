"""Адаптация отчёта повторной проверки к существующим validation_issues.

Валидатор работает по документам версии (``run_validation``), его отчёт —
правила с находками. Здесь находки приводятся к форме, в которой их пишет
существующий конвейер (``review_storage`` → ``validation_issues``), чтобы
повторная проверка после правок ложилась в ту же таблицу и тот же чат.

Уровень замечаний сохраняет политику main: все находки предупреждающие.
``failed`` сохраняется как открытая находка, а ``needs_review`` требует
внимания человека; ни один из этих статусов не означает успешную проверку.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any
from uuid import uuid4

from app.review_logic import digest
from worker.validation.validator import Finding, RuleResult, ValidationReport, load_rules

_STATUS_MAP = {"failed": "open", "needs_review": "needs_review"}


def _issue_id() -> str:
    return str(uuid4())


def findings_to_issues(
    report: ValidationReport, rules_config: Mapping[str, Any] | None = None
) -> list[dict[str, Any]]:
    """Словари-записи для validation_issues по всем находкам отчёта."""

    config = rules_config if rules_config is not None else load_rules()
    rules = {rule.get("id"): rule for rule in config.get("rules", [])}
    issues: list[dict[str, Any]] = []
    for result in report.results:
        for finding in result.findings:
            status = _STATUS_MAP.get(finding.status)
            if status is None:
                continue
            rule = rules.get(result.rule_id, {})
            revision = (
                result.__dict__.get("rule_revision")
                or rule.get("revision")
                or config.get("revision")
                or digest(rule)
            )
            issues.append(
                {
                    "id": _issue_id(),
                    "rule_code": result.rule_id,
                    "rule_revision": str(revision),
                    "type": finding.code,
                    "severity": "warning",
                    "status": status,
                    "details": {
                        "message": finding.message,
                        "evidence": finding.evidence,
                        "rule_name": result.name,
                        "source": "post_edit_validation",
                    },
                }
            )
    return issues


def count_by_severity(issues: list[dict[str, Any]]) -> dict[str, int]:
    """Сводка по серьёзности для ответа чата без чтения всех деталей."""

    counts: dict[str, int] = {}
    for issue in issues:
        counts[issue["severity"]] = counts.get(issue["severity"], 0) + 1
    return counts


__all__ = ["findings_to_issues", "count_by_severity", "Finding", "RuleResult"]
