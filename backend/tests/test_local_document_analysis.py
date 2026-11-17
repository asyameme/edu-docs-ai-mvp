"""Предграфовый анализ настоящим валидатором без нормализации графа."""

from copy import deepcopy
from types import SimpleNamespace
from uuid import uuid4

import pytest
from document_cases import payload

from app.document_analysis import analyze_documents
from app.document_reconciliation import apply_document_mutations
from app.domain import SnapshotV2
from app.review_activities import ReviewActivities
from app.review_contract import AnalysisV2
from app.review_projection import validator_result_for_contract
from app.validation_bridge import run_local_check, validation_implementation_revision
from worker.validation.validator import load_rules


def package():
    rpd, plan = payload("RPD"), payload("CURRICULUM")
    rpd["disciplines"][0]["hours_total"] = 100
    return [{"document_id": str(uuid4()), "parsed": d} for d in (rpd, plan)]


def hours_config():
    config = load_rules()
    rule = next(r for r in config["rules"] if r["id"] == "R-02")
    rule["status"] = "ready"
    return {**config, "rules": [rule]}


def test_real_rule_yields_document_choices_without_building_graph(monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("Предграфовый анализ не должен строить граф")

    monkeypatch.setattr("app.document_adapter.to_snapshot_v2", forbidden)
    documents = package()
    original = deepcopy(documents)
    job_id = uuid4()
    config = hours_config()
    result = analyze_documents(documents, job_id, config=config)
    AnalysisV2.model_validate(validator_result_for_contract(result))
    assert result == analyze_documents(documents, job_id, config=config)
    assert "candidate" not in result
    assert len(result["issues"]) == 1
    issue = result["issues"][0]
    assert issue["requires_decision"]
    assert len(issue["variants"]) == 2
    assert set(issue["source_document_ids"]) == {d["document_id"] for d in documents}
    assert not issue["source_ref_ids"]
    original_by_id = {d["document_id"]: d["parsed"] for d in documents}
    for variant in issue["variants"]:
        resolved = apply_document_mutations(original_by_id, variant["changes"])
        values = [d.get("disciplines", d.get("curriculum_items"))[0]["hours_total"]
                  for d in resolved.values()]
        assert values[0] == values[1]
        again = analyze_documents(
            [{"document_id": i, "parsed": d} for i, d in resolved.items()], uuid4(), config=config
        )
        assert again["coverage"][0]["status"] == "passed"
    assert documents == original


def test_ambiguous_locator_keeps_finding_without_fabricating_choices():
    documents = package()
    documents[0]["parsed"]["disciplines"].append(
        deepcopy(documents[0]["parsed"]["disciplines"][0])
    )
    result = analyze_documents(documents, uuid4(), config=hours_config())
    assert result["issues"]
    assert all(not i["variants"] and not i["requires_decision"] for i in result["issues"])
    assert all(i["evidence"]["document_mapping_limitations"] for i in result["issues"])


def test_duplicate_filenames_are_resolved_by_document_identity():
    documents = package()
    for item in documents:
        item["parsed"]["meta"]["file_name"] = "same.docx"
        for rows in (item["parsed"].get("disciplines", []),
                     item["parsed"].get("curriculum_items", [])):
            for row in rows:
                row["source_ref"]["file"] = "same.docx"
    result = analyze_documents(documents, uuid4(), config=hours_config())
    assert len(result["issues"][0]["variants"]) == 2


def test_missing_nullable_formulation_can_be_filled_from_an_existing_document_value():
    first, second = payload("RPD"), payload("RPP")
    first["competencies"] = [{"code": "УК-1", "formulation": None,
                               "source_ref": {"file": "first.docx", "paragraph_index": 1}}]
    second["competencies"] = [{"code": "УК-1", "formulation": "Проверяемая формулировка",
                                "source_ref": {"file": "second.docx", "paragraph_index": 1}}]
    documents = [{"document_id": str(uuid4()), "parsed": first},
                 {"document_id": str(uuid4()), "parsed": second}]
    result = analyze_documents(documents, uuid4(), config=load_rules())
    issue = next(
        item for item in result["issues"]
        if item["type"] == "competency_formulation_missing"
    )

    assert issue["requires_decision"] is True
    assert len(issue["variants"]) == 1
    changes = issue["variants"][0]["changes"]
    assert len(changes) == 2
    assert any(change["expected_old_value"] is None
               and change["value"] == "Проверяемая формулировка" for change in changes)


def test_pregraph_analysis_runs_all_rules_without_changing_profile():
    documents = package()
    config = load_rules()
    original = deepcopy(config)
    result = analyze_documents(documents, uuid4(), config=config)
    assert {item["rule_code"] for item in result["coverage"]} == {
        rule["id"] for rule in config["rules"]
    }
    assert len(result["coverage"]) == 17
    assert config == original
    assert result["ruleset"]["required_rules"] == ["R-01", "R-08-local-reference"]


def test_graph_check_runs_all_rules_and_keeps_drafts_out_of_required_profile():
    documents = package()
    config = load_rules()
    result = run_local_check(documents, SnapshotV2(), uuid4(), config=config)

    assert {item["rule_code"] for item in result["coverage"]} == {
        rule["id"] for rule in config["rules"]
    }
    assert len(result["coverage"]) == 17
    assert result["ruleset"]["required_rules"] == ["R-01", "R-08-local-reference"]
    assert any(item["rule_code"] == "R-03" for item in result["coverage"])


def test_activity_uses_local_v2_analyzer_and_preserves_protocol(monkeypatch, tmp_path):
    documents = package()
    for item in documents:
        item["sha256"] = item["parsed"]["meta"]["file_hash"]
    job = {"id": uuid4(), "program_id": uuid4(), "input_version_id": uuid4(),
           "report": {}, "parameters": {
               "purpose": "prepare", "executor": "local", "schema_version": 2,
               "local_rules": hours_config(),
               "implementation_revision": validation_implementation_revision(),
               "input": {"documents": documents},
           }}
    published = []

    class Repository:
        def job_by_id(self, identity):
            assert identity == job["id"]
            return job

        def publish(self, identity, result, artifacts, raw, raw_hash):
            assert "candidate" not in raw and "candidate" not in result
            AnalysisV2.model_validate(validator_result_for_contract(raw))
            assert any("evidence" in item for item in raw["issues"])
            assert all("evidence" not in item for item in result["issues"])
            published.append(result)
            job["report"]["outcome"] = "completed"
            return "completed"

    def forbidden(*args, **kwargs):
        pytest.fail("Локальная подготовка V2 не должна нормализовать граф в Activity")

    monkeypatch.setattr("app.review_activities.to_snapshot_v2", forbidden)
    activity = object.__new__(ReviewActivities)
    activity.repo = Repository()
    activity.settings = SimpleNamespace(artifact_files_path=tmp_path)
    assert activity.execute({"job_id": str(job["id"])}) == "completed"
    assert published[0]["schema_version"] == 2
    assert published[0]["issues"][0]["requires_decision"]
    assert activity.execute({"job_id": str(job["id"])}) == "completed"
    assert len(published) == 1


@pytest.mark.parametrize(("rule_revision", "config_revision", "expected"), [
    ("rule-7", "profile-4", "rule-7"),
    (None, "profile-4", "profile-4"),
])
def test_explicit_revision_is_preserved_on_both_phases(rule_revision, config_revision, expected):
    documents = package()
    config = hours_config()
    config["revision"] = config_revision
    if rule_revision:
        config["rules"][0]["revision"] = rule_revision
    analysis = analyze_documents(documents, uuid4(), config=config)
    checked = run_local_check(documents, SnapshotV2(), uuid4(), config=config)
    for result in (analysis, checked):
        assert result["coverage"][0]["rule_revision"] == expected
        assert result["issues"][0]["rule_revision"] == expected
