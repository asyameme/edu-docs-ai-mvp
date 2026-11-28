"""Политика замечаний и целостности, отделённая от результата валидатора."""

from copy import deepcopy

from app.review_logic import digest

CONFIRMATION_POLICY = {
    "revision": "mvp-version-policy-v2",
    "normative_findings": "warning",
    "warning_confirmation": "not_required",
    "graph_integrity_errors": "blocking",
    "required_coverage": "must_have_final_result",
}


def issue_severity(issue):
    if str(issue.get("rule_code", "")).startswith("BACKEND_GRAPH_"):
        return "blocking"
    return "warning"


def apply_confirmation_policy(result):
    """Закрепляет политику в ревизии результата, не меняя статус покрытия."""
    projected = deepcopy(result)
    ruleset = projected.get("ruleset")
    if not isinstance(ruleset, dict) or not isinstance(ruleset.get("revision"), str):
        raise ValueError("Результат не содержит ревизию набора правил")
    marker = CONFIRMATION_POLICY["revision"] + ":"
    if not ruleset["revision"].startswith(marker):
        upstream_revision = ruleset["revision"]
        upstream_ruleset = {
            key: value for key, value in ruleset.items() if key != "revision"
        }
        ruleset["revision"] = marker + digest({
            "upstream_revision": upstream_revision,
            "upstream_ruleset": upstream_ruleset,
            "policy": CONFIRMATION_POLICY,
        })
    for issue in projected.get("issues", []):
        issue["severity"] = issue_severity(issue)
    return projected
