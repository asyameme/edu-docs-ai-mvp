"""Разделение готовых проверок по стадиям без изменения правил и статусов."""

import json

import pytest

from worker.validation.validator import ValidationError, load_rules, run_validation


@pytest.mark.parametrize("phase", ["document_reconciliation", "graph_validation"])
def test_backend_r04_uses_configured_factor_and_reports_mismatch(phase):
    from app.validation_bridge import run_document_rules

    config = load_rules()
    rule = next(rule for rule in config["rules"] if rule["id"] == "R-04")
    config["rules"] = [rule]
    row = {"hours_total": 108, "credit_units": 3}
    documents = [{"meta": {"doc_type": "RPD"}, "disciplines": [row]}]
    assert run_document_rules(documents, config, phase=phase)["results"][0]["status"] == "passed"
    row["hours_total"] = 100
    result = run_document_rules(documents, config, phase=phase)["results"][0]
    assert result["status"] == "failed"
    assert result["findings"][0]["code"] == "credit_hours_mismatch"
    assert result["findings"][0]["evidence"]["hours_per_credit_unit"] == 36
    rule["inputs"]["hours_per_credit_unit"] = 40
    row["hours_total"] = 120
    assert run_document_rules(documents, config, phase=phase)["results"][0]["status"] == "passed"


@pytest.mark.parametrize("phase", ["document_reconciliation", "graph_validation"])
@pytest.mark.parametrize("different", [False, True])
def test_backend_r08_semantic_preserves_manual_review_on_model_failure(
    phase, different, monkeypatch,
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

    result = run_document_rules(documents, config, phase=phase)["results"][0]

    assert result["rule_id"] == "R-08-semantic"
    assert result["status"] == ("needs_review" if different else "passed")
    assert len(calls) == int(different)
    if different:
        assert result["findings"]
        assert all(finding["status"] == "needs_review" for finding in result["findings"])


def test_all_current_rules_have_both_explicit_phases():
    config = load_rules()
    assert config["phases"] == {
        "document_reconciliation": {"input": "parsed_documents"},
        "graph_validation": {"input": "graph_projection"},
    }
    assert len(config["rules"]) == 17
    assert all(
        rule["phases"] == ["document_reconciliation", "graph_validation"]
        for rule in config["rules"]
    )
    semantic_indicators = next(r for r in config["rules"] if r["id"] == "R-08-semantic")
    assert "graph_validation" in semantic_indicators["phases"]


def test_engine_selects_rule_phase_and_reports_it():
    rules = {
        "schema_version": 2,
        "phases": {
            "document_reconciliation": {"input": "parsed_documents"},
            "graph_validation": {"input": "graph_projection"},
        },
        "rules": [
            {"id": "before", "type": "unique_by_fields", "status": "ready",
                 "phases": ["document_reconciliation"], "inputs": {"collections": [{
                     "records_path": "$.competencies[*]", "key_fields": ["code"]
                 }]}},
            {"id": "after", "type": "unique_by_fields", "status": "ready",
                 "phases": ["graph_validation"], "inputs": {"collections": [{
                     "records_path": "$.competencies[*]", "key_fields": ["code"]
                 }]}},
        ],
    }
    documents = [{"meta": {"doc_type": "RPD"}, "competencies": [{"code": "УК-1"}]}]

    before = run_validation(documents, rules, phase="document_reconciliation")
    after = run_validation(documents, rules, phase="graph_validation")

    assert [item.rule_id for item in before.results] == ["before"]
    assert [item.rule_id for item in after.results] == ["after"]
    assert before.to_dict()["phase"] == "document_reconciliation"
    assert after.to_dict()["phase"] == "graph_validation"


def test_engine_rejects_unknown_phase():
    with pytest.raises(ValidationError, match="Неизвестная фаза"):
        run_validation([], phase="before_graph")


@pytest.mark.parametrize("phase", ["document_reconciliation", "graph_validation"])
def test_draft_handlers_are_not_called_but_ready_handlers_run(phase, monkeypatch):
    from worker.validation.validator import HANDLERS

    config = load_rules()
    rule = {
        "id": "draft-probe", "type": "draft-probe", "status": "draft",
        "phases": [phase],
    }
    config["rules"] = [rule]
    calls = []

    def forbidden(*args):
        calls.append(True)
        raise RuntimeError("Черновой обработчик не должен вызываться")

    monkeypatch.setitem(HANDLERS, "draft-probe", forbidden)
    assert run_validation([], config, phase=phase).results == []
    assert calls == []
    rule["status"] = "ready"
    with pytest.raises(RuntimeError, match="не должен"):
        run_validation([], config, phase=phase)
    assert calls == [True]


def test_load_rules_rejects_unhashable_phase_values_as_configuration_error(tmp_path):
    config = {
        "schema_version": 2,
        "phases": {
            "document_reconciliation": {"input": "parsed_documents"},
            "graph_validation": {"input": "graph_projection"},
        },
        "rules": [{"id": "broken", "status": "ready",
                   "phases": ["document_reconciliation", {}]}],
    }
    path = tmp_path / "rules.json"
    path.write_text(json.dumps(config), encoding="utf-8")

    with pytest.raises(ValidationError, match="Правило broken"):
        load_rules(path)
