"""Проверка изменённого снимка настоящим валидатором через Backend Activity."""

from copy import deepcopy
from uuid import uuid4

from app.domain import SnapshotV2
from app.review_activities import ReviewActivities
from app.validation_bridge import (
    run_local_check,
    validation_implementation_revision,
    validation_ruleset,
)
from worker.validation.validator import Document


def test_activity_checks_changed_hours_and_does_not_publish_twice():
    document_id, entity_id, ref_id, job_id = [uuid4() for _ in range(4)]
    original = {"document_id": str(document_id), "parsed": {
        "meta": {"doc_type": "RPD"},
        "disciplines": [{
            "name": "Алгебра", "credit_units": 4, "hours_total": 108,
            "source_ref": {"row": 1},
        }],
    }}
    snapshot = SnapshotV2.model_validate({
        "document_ids": [document_id],
        "entities": [{"id": entity_id, "kind": "discipline",
                      "attributes": {"hours_total": 144}, "source_ref_ids": [ref_id]}],
        "source_refs": [{"id": ref_id, "document_id": document_id, "locator": '{"row":1}'}],
        "source_links": [{"id": uuid4(), "target_kind": "entity", "target_id": entity_id,
                          "field_path": "/attributes/hours_total", "source_ref_id": ref_id,
                          "observed_value": {"value": 108}}],
    })
    config = {"schema_version": 2,
              "phases": {"document_reconciliation": {"input": "parsed_documents"},
                         "graph_validation": {"input": "graph_projection"}},
              "rules": [{
        "id": "hours", "name": "Часы и ЗЕТ", "status": "ready", "type": "credit_hours",
        "phases": ["document_reconciliation", "graph_validation"],
    }]}
    ruleset = validation_ruleset(config, [Document(original["parsed"], "rpd")])
    baseline = run_local_check([original], snapshot, job_id, config=config, ruleset=ruleset)
    assert baseline["coverage"][0]["status"] == "failed"
    assert baseline["coverage"][0]["checks"] == 1
    job = {"id": job_id, "report": {}, "parameters": {
        "purpose": "check", "executor": "local", "local_rules": config,
        "implementation_revision": validation_implementation_revision(),
        "input": {"documents": [original], "snapshot": snapshot.model_dump(mode="json"),
                  "ruleset": ruleset},
    }}

    class Repository:
        def __init__(self):
            self.results = []
            self.raw_results = []

        def job_by_id(self, identity):
            assert identity == job_id
            return job

        def publish(self, identity, result, artifacts, raw, raw_hash):
            self.results.append(deepcopy(result))
            self.raw_results.append(deepcopy(raw))
            job["report"]["outcome"] = "completed"
            return "completed"

    activity = object.__new__(ReviewActivities)
    activity.repo = Repository()
    assert activity.execute({"job_id": str(job_id)}) == "completed"
    assert activity.repo.results[0]["coverage"][0]["status"] == "passed"
    assert "checks" not in activity.repo.results[0]["coverage"][0]
    assert "checks" in activity.repo.raw_results[0]["coverage"][0]
    assert activity.repo.raw_results[0]["coverage"][0]["checks"] == 1
    assert all("evidence" not in issue and "checks" not in issue
               for issue in activity.repo.results[0]["issues"])
    assert original["parsed"]["disciplines"][0]["hours_total"] == 108
    assert activity.execute({"job_id": str(job_id)}) == "completed"
    assert len(activity.repo.results) == 1
