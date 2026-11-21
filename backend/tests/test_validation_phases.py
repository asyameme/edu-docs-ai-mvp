"""Проверки запускаются только на назначенной им стадии."""

import json

import pytest

from worker.validation.validator import ValidationError, load_rules, run_validation


def test_backend_r04_uses_configured_factor_and_reports_mismatch():
    from app.validation_bridge import run_document_rules

    config = load_rules()
    rule = next(rule for rule in config["rules"] if rule["id"] == "R-04")
    config["rules"] = [rule]
    row = {"hours_total": 108, "credit_units": 3}
    documents = [{"meta": {"doc_type": "RPD"}, "disciplines": [row]}]
    assert run_document_rules(documents, config, phase="graph_validation")["results"][0]["status"] == "passed"
    row["hours_total"] = 100
    result = run_document_rules(documents, config, phase="graph_validation")["results"][0]
    assert result["status"] == "failed"
    assert result["findings"][0]["code"] == "credit_hours_mismatch"
    assert result["findings"][0]["evidence"]["hours_per_credit_unit"] == 36
    rule["inputs"]["hours_per_credit_unit"] = 40
    row["hours_total"] = 120
    assert run_document_rules(documents, config, phase="graph_validation")["results"][0]["status"] == "passed"


@pytest.mark.parametrize("different", [False, True])
def test_backend_r08_deterministic_choices_do_not_call_model(
    different, monkeypatch,
):
    from app.validation_bridge import run_document_rules

    calls = []

    def unavailable(*args):
        calls.append(True)
        raise RuntimeError("Тест: модель недоступна")

    monkeypatch.setattr("app.validation_bridge._ValidationBudget.semantic", unavailable)
    config = load_rules()
    config["rules"] = [rule for rule in config["rules"] if rule["id"] == "R-08-semantic"]
    documents = [{
        "meta": {"doc_type": kind, "program_id": "P"},
        "competencies": [{"code": "A", "formulation": "Компетенция"}],
        "indicators": [{"code": "A.1", "competency_code": "A", "text": text}],
    } for kind, text in [("RPD", "Индикатор"),
                         ("CKMV", "Другой смысл" if different else "Индикатор")]]

    result = run_document_rules(documents, config, phase="document_reconciliation")["results"][0]

    assert result["rule_id"] == "R-08-semantic"
    assert result["status"] == ("needs_review" if different else "passed")
    assert calls == []
    if different:
        assert result["findings"]
        assert all(finding["status"] == "needs_review" for finding in result["findings"])
