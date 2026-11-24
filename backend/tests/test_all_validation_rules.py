"""Приёмка полного локального профиля на исходниках проекта и известных дефектах."""

from copy import deepcopy
from pathlib import Path
from uuid import uuid4

import pytest

from app.document_analysis import analyze_documents
from app.domain import SnapshotV2
from app.local_parser import parse_local_document
from app.parser_adapter import to_snapshot
from app.review_contract import AnalysisV2, CheckResult
from app.review_projection import validator_result_for_contract
from app.validation_bridge import project_snapshot_documents, run_local_check
from worker.validation.validator import load_rules, run_validation

ORIGINALS = Path(__file__).resolve().parents[2] / "tests" / "originals"


def test_backend_runs_only_active_rules_on_real_documents(monkeypatch):
    monkeypatch.setattr("app.validation_bridge.VALIDATION_MAX_LLM_CALLS", 0)
    program_id = uuid4()
    members = [
        "РПД/РПД_матанализ1.docx", "РПД/РПД_БД.docx", "РПП/РПП.docx",
        "Учебный план/UP.docx", "Характеристика/Общая характеристика.docx", "ЦКМВ/ЦКМВ.docx",
    ]
    documents = [
        {"document_id": uuid4(), "output": parse_local_document(
            name, (ORIGINALS / Path(name).name).read_bytes(), program_id
        )}
        for name in members
    ]
    snapshot_data, _ = to_snapshot(program_id, documents)
    snapshot = SnapshotV2.model_validate(snapshot_data)
    inputs = [{"document_id": str(d["document_id"]), "parsed": d["output"]} for d in documents]
    original = deepcopy(inputs)
    config = load_rules()
    original_config = deepcopy(config)
    analysis = analyze_documents(inputs, uuid4(), config=config)
    projected = project_snapshot_documents(inputs, snapshot)
    result = run_local_check(projected, snapshot, uuid4(), config=config)
    coverage = {entry["rule_code"]: entry for entry in result["coverage"]}
    all_rules = {
        rule["id"] for rule in config["rules"]
        if rule["status"] != "draft"
        and "graph_validation" in rule["phases"]
        and rule.get("run_mode", "automatic") == "automatic"
    }
    assert set(coverage) == all_rules
    assert len(coverage) == 13
    assert coverage["R-03"]["status"] == "passed"
    assert coverage["R-03-RPP"]["status"] == "passed"
    assert coverage["R-05"]["status"] == "passed"
    assert coverage["R-05-title"]["status"] == "passed"
    assert coverage["R-06"]["status"] == "passed"
    assert coverage["R-02"]["status"] == "passed"
    assert all(issue["rule_code"] in all_rules for issue in result["issues"])
    assert result["ruleset"]["required_rules"] == [
        "R-01-graph", "R-08-local-reference", "R-04",
    ]
    assert all(entry["checks"] > 0 for entry in coverage.values() if entry["status"] == "passed")
    selected = {"R-10", "R-13", "R-14", "R-15"}
    assert all(rule["status"] == "ready" for rule in config["rules"]
               if rule["id"] in selected)
    AnalysisV2.model_validate(validator_result_for_contract(analysis))
    CheckResult.model_validate(validator_result_for_contract(result))
    for phase_result, phase in (
        (analysis, "document_reconciliation"), (result, "graph_validation")
    ):
        phase_coverage = {entry["rule_code"]: entry for entry in phase_result["coverage"]}
        assert set(phase_coverage) == {
            rule["id"] for rule in config["rules"]
            if rule["status"] != "draft" and phase in rule["phases"]
            and rule.get("run_mode", "automatic") == "automatic"
        }
        assert {"R-09", "R-11"}.isdisjoint(phase_coverage)
        assert phase_result["ruleset"]["required_rules"] == (
            [] if phase == "document_reconciliation"
            else ["R-01-graph", "R-08-local-reference", "R-04"]
        )
        for code in selected & phase_coverage.keys():
            assert phase_coverage[code]["status"] == "passed", phase_coverage[code]
            assert phase_coverage[code]["checks"] > 0
    assert inputs == original
    assert config == original_config
    print("\nРеальные документы:", {
        code: (item["status"], item["checks"]) for code, item in coverage.items()
    })


@pytest.mark.parametrize(("rule_id", "documents", "field", "bad", "good"), [
    ("R-06", [{"meta": {"doc_type": "rpd"}, "competencies": [{"code": "A"}, {"code": "A"}]}],
     (0, "competencies", 1, "code"), "A", "B"),
    ("R-08-local-reference", [{"meta": {"doc_type": "rpd"}, "competencies": [{"code": "A"}],
                              "indicators": [{"competency_code": "B"}]}],
     (0, "indicators", 0, "competency_code"), "B", "A"),
    ("R-13", [{"meta": {"doc_type": "ckmv"},
               "document_sections": [{"title": "Содержание", "text": ""}]}],
     (0, "document_sections", 0, "text"), "", "Непустое содержание"),
    ("R-14", [{"meta": {"doc_type": "characteristic", "program_id": "P"},
               "program": {"education_languages": ["русский"]}},
              {"meta": {"doc_type": "rpd", "program_id": "P"},
               "education_languages": ["английский"]}],
     (1, "education_languages", 0), "английский", "русский"),
])
def test_draft_detects_known_error_then_accepts_correction(rule_id, documents, field, bad, good):
    config = load_rules()
    rule = next(item for item in config["rules"] if item["id"] == rule_id)
    assert rule["status"] in {"draft", "ready"}
    # Проверка обработчика черновика отдельно от штатного профиля.
    rule["status"] = "ready"
    if rule_id == "R-13":
        rule["inputs"]["required_sections"] = ["Содержание"]
    config["rules"] = [rule]
    docs = deepcopy(documents)
    target = docs
    for key in field[:-1]:
        target = target[key]
    assert target[field[-1]] == bad
    failed = run_validation(docs, config, phase="graph_validation").results[0]
    assert failed.status == "failed" and failed.checks > 0 and failed.findings
    target[field[-1]] = good
    passed = run_validation(docs, config, phase="graph_validation").results[0]
    assert passed.status == "passed" and passed.checks > 0 and not passed.findings
