"""Тесты адаптера «отчёт валидатора → записи validation_issues»."""

from worker.agent.validation_adapter import count_by_severity, findings_to_issues
from worker.validation.validator import Finding, RuleResult, ValidationReport


def _report():
    return ValidationReport(
        results=[
            RuleResult(
                rule_id="R-03",
                name="Внутренняя сумма часов",
                status="failed",
                checks=3,
                findings=[
                    Finding(
                        code="hours_mismatch",
                        status="failed",
                        message="Сумма 96 не равна 108",
                        evidence={"discipline": "MLOps"},
                    ),
                    Finding(
                        code="R-03",
                        status="needs_review",
                        message="Возможное расхождение",
                        evidence={"discipline": "Анализ данных"},
                    ),
                    Finding(code="R-03", status="passed", message="Ок", evidence={}),
                ],
            ),
            RuleResult(
                rule_id="R-05", name="ЗЕТ РПД и план", status="passed", checks=10, findings=[]
            ),
        ]
    )


def test_findings_map_to_issue_records():
    config = {"revision": "3", "rules": [{"id": "R-03", "type": "sum_components_equals_total"}]}
    issues = findings_to_issues(_report(), config)
    assert len(issues) == 2
    by_status = {issue["status"]: issue for issue in issues}
    assert set(by_status) == {"open", "needs_review"}
    assert by_status["open"]["rule_code"] == "R-03"
    assert by_status["open"]["type"] == "hours_mismatch"
    assert by_status["open"]["severity"] == "blocking"
    assert by_status["needs_review"]["severity"] == "warning"
    assert by_status["open"]["rule_revision"] == "3"
    assert by_status["needs_review"]["details"]["message"] == "Возможное расхождение"


def test_severity_counts_for_chat_summary():
    issues = findings_to_issues(_report(), {"rules": []})
    assert count_by_severity(issues) == {"blocking": 1, "warning": 1}
