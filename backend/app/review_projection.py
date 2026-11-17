"""Преобразование внутренних результатов проверки в публичный контракт Backend."""

from copy import deepcopy

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
}
_COVERAGE_FIELDS = {"rule_code", "rule_revision", "status", "reason"}


def _project_result(result):
    projected = deepcopy(result)
    for item in projected.get("coverage", []):
        if item.get("status") == "needs_review":
            item["status"] = "failed"
        for key in tuple(item):
            if key not in _COVERAGE_FIELDS:
                item.pop(key)
    projected["issues"] = [
        {key: value for key, value in item.items() if key in _ISSUE_FIELDS}
        for item in projected.get("issues", [])
    ]
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
    public_issue = {key: value for key, value in issue.items() if key in _ISSUE_FIELDS}
    return ReviewIssue.model_validate(public_issue).model_dump(mode="json")
