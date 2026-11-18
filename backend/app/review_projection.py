"""Преобразование внутренних результатов проверки в публичный контракт Backend."""

from copy import deepcopy

from app.confirmation_policy import issue_severity
from app.review_contract import ReviewIssue

_ISSUE_FIELDS = {
    "id",
    "rule_code",
    "rule_revision",
    "type",
    "severity",
    "status",
    "message",
    "source_ref_ids",
    "source_document_ids",
    "requires_decision",
    "variants",
    "rpd_alignment",
}
_COVERAGE_FIELDS = {"rule_code", "rule_revision", "status", "reason"}


def _rpd_alignment_summary(issue):
    if issue.get("rule_code") != "R-11":
        return None
    source = issue.get("rpd_alignment")
    if not isinstance(source, dict):
        source = issue.get("evidence")
    if not isinstance(source, dict):
        source = {}
    classification = source.get("classification")
    if classification not in ("supported", "gap", "uncertain", "invalid"):
        classification = None
    return {
        "outcome_id": (
            source.get("outcome_id") if isinstance(source.get("outcome_id"), str) else None
        ),
        "outcome_text": (
            source.get("outcome_text")
            if isinstance(source.get("outcome_text"), str)
            else None
        ),
        "classification": classification,
        "reason": source.get("reason") if isinstance(source.get("reason"), str) else None,
    }


def _project_issue(issue):
    public_issue = {key: value for key, value in issue.items() if key in _ISSUE_FIELDS}
    public_issue["rpd_alignment"] = _rpd_alignment_summary(issue)
    public_issue["severity"] = issue_severity(issue)
    return public_issue


def _project_result(result):
    projected = deepcopy(result)
    for item in projected.get("coverage", []):
        if item.get("status") == "needs_review":
            item["status"] = "failed"
        for key in tuple(item):
            if key not in _COVERAGE_FIELDS:
                item.pop(key)
    projected["issues"] = [_project_issue(item) for item in projected.get("issues", [])]
    return projected


def validator_result_for_contract(result):
    """Нормализует результат локального валидатора, оставляя исходник для аудита."""
    return _project_result(result)


def project_legacy_review_result(result):
    """Убирает поля старого расширения из результата, уже сохранённого в базе."""
    return _project_result(result)


def project_prepare_report(result):
    """Скрывает внутренние поля сборки графа в публичном отчёте подготовки."""
    projected = project_legacy_review_result(result)
    return {
        key: value
        for key, value in projected.items()
        if key not in {"candidate", "artifacts", "graph_ready", "graph_build_issues"}
    }


def project_legacy_review_issue(issue):
    """Возвращает сохранённое замечание в формате, совместимом с контрактом main."""
    return ReviewIssue.model_validate(_project_issue(issue)).model_dump(mode="json")
