"""Разделение готовых проверок по стадиям без изменения правил и статусов."""

import json

import pytest

from worker.validation.validator import ValidationError, load_rules, run_validation


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


def test_load_rules_rejects_unhashable_phase_values_as_configuration_error(tmp_path):
    config = {
        "schema_version": 2,
        "phases": {
            "document_reconciliation": {"input": "parsed_documents"},
            "graph_validation": {"input": "graph_projection"},
        },
        "rules": [{"id": "broken", "phases": ["document_reconciliation", {}]}],
    }
    path = tmp_path / "rules.json"
    path.write_text(json.dumps(config), encoding="utf-8")

    with pytest.raises(ValidationError, match="Правило broken"):
        load_rules(path)
