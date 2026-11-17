from copy import deepcopy
from uuid import uuid4

import pytest
from pydantic import ValidationError

from app.review_contract import CheckResult, ReviewIssue, RuleCoverage
from app.review_projection import (
    project_legacy_review_issue,
    project_legacy_review_result,
    validator_result_for_contract,
)


def sample_result():
    return {
        "ruleset": {
            "id": "local",
            "revision": "r1",
            "config": {},
            "required_rules": ["R-01"],
            "required_document_types": ["rpd"],
        },
        "coverage": [{
            "rule_code": "R-01",
            "rule_revision": "r1",
            "status": "needs_review",
            "checks": 2,
            "reason": "Требуется ручная проверка",
        }],
        "issues": [{
            "id": str(uuid4()),
            "rule_code": "R-01",
            "rule_revision": "r1",
            "type": "manual_review",
            "severity": "warning",
            "status": "needs_review",
            "message": "Нужна ручная проверка",
            "source_ref_ids": [],
            "evidence": {"reason": "Низкая уверенность"},
            "checks": 2,
        }],
    }


def test_main_contract_does_not_accept_validator_only_fields_or_coverage_status():
    coverage = {
        "rule_code": "R-01",
        "rule_revision": "r1",
        "status": "failed",
    }
    with pytest.raises(ValidationError):
        RuleCoverage.model_validate({**coverage, "checks": 2})
    with pytest.raises(ValidationError):
        RuleCoverage.model_validate({**coverage, "status": "needs_review"})
    issue = {
            "id": uuid4(),
            "rule_code": "R-01",
            "rule_revision": "r1",
            "type": "manual_review",
            "severity": "warning",
            "status": "needs_review",
            "message": "Нужна ручная проверка",
        }
    for extension in ({"evidence": {}}, {"checks": 1}):
        with pytest.raises(ValidationError):
            ReviewIssue.model_validate({**issue, **extension})


def test_validator_projection_maps_review_status_and_keeps_raw_diagnostics():
    raw = sample_result()
    original = deepcopy(raw)

    projected = validator_result_for_contract(raw)
    parsed = CheckResult.model_validate(projected)

    assert raw == original
    assert parsed.coverage[0].status == "failed"
    assert parsed.issues[0].status == "needs_review"
    assert parsed.issues[0].severity == "warning"
    assert "checks" not in projected["coverage"][0]
    assert "checks" not in projected["issues"][0]
    assert "evidence" not in projected["issues"][0]


def test_legacy_report_and_database_issue_are_projected_to_main_contract():
    raw = sample_result()
    raw["graph_ready"] = True
    raw["graph_build_issues"] = []

    projected = project_legacy_review_result(raw)
    issue = project_legacy_review_issue(raw["issues"][0])

    assert projected["graph_ready"] is True
    assert projected["coverage"][0]["status"] == "failed"
    assert "checks" not in projected["coverage"][0]
    assert "evidence" not in projected["issues"][0]
    assert "checks" not in issue
    assert "evidence" not in issue
    CheckResult.model_validate({key: projected[key] for key in ("ruleset", "coverage", "issues")})
