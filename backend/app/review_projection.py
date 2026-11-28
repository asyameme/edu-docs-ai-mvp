"""Преобразование внутренних результатов проверки в публичный контракт Backend."""

from copy import deepcopy

from app.confirmation_policy import issue_severity
from app.review_contract import ReviewIssue
from worker.validation.text_normalization import (
    normalize_variant_formulation_text,
)

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


def _short(value, limit=200):
    return value if isinstance(value, str) and len(value) <= limit else (
        value[:limit] if isinstance(value, str) else None
    )


def _evidence_list(source, key, fields, limit=5):
    items = source.get(key)
    if not isinstance(items, list):
        return None
    result = []
    for item in items:
        if not isinstance(item, dict):
            continue
        row = {}
        for field in fields:
            value = item.get(field)
            if isinstance(value, str) and value.strip():
                row[field] = _short(value)
            elif value is not None and not isinstance(value, str):
                row[field] = value
        if row:
            result.append(row)
        if len(result) >= limit:
            break
    return result or None


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
        "course_evidence": _evidence_list(
            source, "course_evidence", ("id", "topic", "activity")
        ),
        "assessment_evidence": _evidence_list(
            source, "assessment_evidence",
            ("id", "indicator_code", "indicator_text", "assessment_tool"),
        ),
    }


def _variant_text_key(variant):
    if not isinstance(variant, dict):
        return None
    changes = variant.get("changes")
    if not isinstance(changes, list) or not changes:
        return None
    values = []
    for change in changes:
        if not isinstance(change, dict) or change.get("op") != "set_document_value":
            return None
        field = str(change.get("field_path", "")).rsplit("/", 1)[-1]
        value = change.get("value")
        if field not in {"formulation", "text"} or not isinstance(value, str):
            return None
        values.append((field, normalize_variant_formulation_text(value)))
    return values[0] if all(value == values[0] for value in values) else None


def _distinct_text_variants(issue, selected_variant_id=None):
    variants = issue.get("variants")
    if not isinstance(variants, list):
        return variants
    result = []
    indexes = {}
    for variant in variants:
        key = _variant_text_key(variant)
        if key is None:
            result.append(variant)
            continue
        if key not in indexes:
            indexes[key] = len(result)
            result.append(variant)
        elif variant.get("id") == selected_variant_id:
            result[indexes[key]] = variant
    return result


def _project_issue(issue, selected_variant_id=None):
    public_issue = {key: value for key, value in issue.items() if key in _ISSUE_FIELDS}
    if "variants" in issue:
        public_issue["variants"] = _distinct_text_variants(issue, selected_variant_id)
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


def project_legacy_review_issue(issue, selected_variant_id=None):
    """Возвращает сохранённое замечание в формате, совместимом с контрактом main."""
    return ReviewIssue.model_validate(
        _project_issue(issue, selected_variant_id)
    ).model_dump(mode="json")


def visible_prepare_findings(issues, review_id):
    """Сохраняет карточки принятых вариантов при ранней сборке исходного графа."""
    return [
        item for item in issues
        if not (
            str(item["job_id"]) == str(review_id)
            and str(item["rule_code"]).startswith("R-08")
            and not item.get("requires_decision")
        )
    ]
