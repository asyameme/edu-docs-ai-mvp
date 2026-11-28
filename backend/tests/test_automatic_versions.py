"""Исходная версия и её выгрузка не требуют принятия предметных замечаний."""

from copy import deepcopy
from uuid import uuid4

import pytest
from pydantic import ValidationError

from app.domain import SnapshotV2
from app.errors import DomainError
from app.generation_bridge import approved_changes
from app.generation_repository import GenerationRepository
from app.review_logic import content_hash


def automatic_snapshot():
    program_id, version_id, review_id, document_id, entity_id, ref_id = [uuid4() for _ in range(6)]
    data = {
        "document_ids": [str(document_id)],
        "entities": [
            {
                "id": str(entity_id),
                "kind": "competency",
                "attributes": {"code": "УК-1", "formulation": "Исходный текст"},
                "source_ref_ids": [str(ref_id)],
            }
        ],
        "source_refs": [
            {
                "id": str(ref_id),
                "document_id": str(document_id),
                "locator": '{"table":1,"row":1,"column":1}',
            }
        ],
        "source_links": [
            {
                "id": str(uuid4()),
                "target_kind": "entity",
                "target_id": str(entity_id),
                "field_path": "/attributes/formulation",
                "source_ref_id": str(ref_id),
                "observed_value": {"value": "Исходный текст"},
            }
        ],
        "review": {
            "review_id": str(review_id),
            "import_job_id": str(uuid4()),
            "analysis_version_id": str(version_id),
            "graph_ready": True,
            "lifecycle_policy": "automatic_source_v2",
        },
    }
    return program_id, version_id, data


def checked_job(program_id, version_id, data):
    ruleset = {
        "id": "test",
        "revision": "test-v1",
        "config": {},
        "required_rules": ["R-04"],
        "required_document_types": ["rpd"],
    }
    return {
        "id": uuid4(),
        "program_id": program_id,
        "input_version_id": version_id,
        "parameters": {
            "purpose": "check",
            "review_id": data["review"]["review_id"],
            "ruleset": ruleset,
            "content_hash": content_hash(SnapshotV2.model_validate(data)),
        },
        "report": {
            "outcome": "completed",
            "result": {
                "ruleset": deepcopy(ruleset),
                "coverage": [
                    {
                        "rule_code": "R-04",
                        "rule_revision": "test-v1",
                        "status": "failed",
                    }
                ],
                "issues": [
                    {
                        "id": str(uuid4()),
                        "rule_code": "R-04",
                        "rule_revision": "test-v1",
                        "type": "credit_hours_mismatch",
                        "severity": "warning",
                        "status": "open",
                        "message": "Часы расходятся",
                        "source_ref_ids": [],
                        "requires_decision": False,
                        "variants": [],
                    }
                ],
            },
        },
    }


class JobCursor:
    def __init__(self, job):
        self.job = job

    def execute(self, query, parameters):
        return self

    def fetchone(self):
        return self.job


def read_generation_snapshot(monkeypatch, data, job, program_id, version_id):
    monkeypatch.setattr("app.generation_repository.load_graph", lambda *args: deepcopy(data))
    return GenerationRepository._checked_snapshot(
        JobCursor(job),
        program_id,
        version_id,
        uuid4(),
        data["review"],
    )


def test_version_process_is_automatic_by_default():
    _, _, data = automatic_snapshot()
    assert SnapshotV2.model_validate(data).review.lifecycle_policy == "automatic_source_v2"
    del data["review"]["lifecycle_policy"]
    assert SnapshotV2.model_validate(data).review.lifecycle_policy == "automatic_source_v2"
    data["review"]["lifecycle_policy"] = "unknown"
    with pytest.raises(ValidationError):
        SnapshotV2.model_validate(data)


def test_generation_with_warning_does_not_create_or_require_confirmation(monkeypatch):
    program_id, version_id, data = automatic_snapshot()
    job = checked_job(program_id, version_id, data)
    original = deepcopy(data)

    snapshot = read_generation_snapshot(monkeypatch, data, job, program_id, version_id)

    assert snapshot.review.confirmation is None
    assert data == original
    assert approved_changes(snapshot) == []
    assert job["report"]["result"]["issues"][0]["status"] == "open"


@pytest.mark.parametrize(
    "case",
    ["missing", "pending", "failed", "other_version", "other_review", "manual", "hash", "ruleset"],
)
def test_unverified_or_wrong_check_cannot_authorize_automatic_generation(monkeypatch, case):
    program_id, version_id, data = automatic_snapshot()
    job = checked_job(program_id, version_id, data)
    if case == "missing":
        job = None
    elif case in {"pending", "failed"}:
        job["report"]["outcome"] = case
    elif case == "other_version":
        job["input_version_id"] = uuid4()
    elif case == "other_review":
        job["parameters"]["review_id"] = str(uuid4())
    elif case == "manual":
        job["parameters"]["manual_rule_codes"] = ["R-11"]
    elif case == "hash":
        job["parameters"]["content_hash"] = "0" * 64
    elif case == "ruleset":
        job["report"]["result"]["ruleset"]["revision"] = "changed"

    with pytest.raises(DomainError):
        read_generation_snapshot(monkeypatch, data, job, program_id, version_id)


def test_missing_required_result_is_not_a_passed_check(monkeypatch):
    program_id, version_id, data = automatic_snapshot()
    job = checked_job(program_id, version_id, data)
    job["report"]["result"]["coverage"][0]["status"] = "skipped"
    job["report"]["result"]["issues"] = []
    with pytest.raises(DomainError):
        read_generation_snapshot(monkeypatch, data, job, program_id, version_id)


def test_version_without_marker_still_requires_its_exact_check(monkeypatch):
    program_id, version_id, data = automatic_snapshot()
    del data["review"]["lifecycle_policy"]
    with pytest.raises(DomainError, match="проверка"):
        read_generation_snapshot(monkeypatch, data, None, program_id, version_id)
