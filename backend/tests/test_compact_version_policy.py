"""Компактная сводка должна повторять важные решения полной проверки версии."""

from uuid import uuid4

from app.domain import ReviewState, SnapshotV2
from app.review_contract import CheckResult, Ruleset
from app.review_logic import content_hash, digest
from app.version_policy import compact_version_capabilities, version_capabilities


def _checked(snapshot, version_id, program_id):
    ruleset = Ruleset(
        id="rules", revision="1", config={}, required_rules=[],
        required_document_types=["RPD"],
    )
    result = CheckResult(ruleset=ruleset, coverage=[], issues=[]).model_dump(mode="json")
    return {
        "id": uuid4(),
        "program_id": program_id,
        "input_version_id": version_id,
        "parameters": {
            "purpose": "check", "review_id": str(snapshot.review.review_id),
            "content_hash": content_hash(snapshot), "ruleset": ruleset.model_dump(mode="json"),
        },
        "report": {"outcome": "completed", "result": result},
    }


def test_compact_capabilities_match_full_check_for_a_valid_sealed_version():
    program_id, version_id = uuid4(), uuid4()
    snapshot = SnapshotV2(metadata={"unresolved_relations": ["one"]}, review=ReviewState(
        review_id=uuid4(), import_job_id=uuid4(), analysis_version_id=version_id,
        lifecycle_policy="automatic_source_v2",
    ))
    checked = _checked(snapshot, version_id, program_id)

    full = version_capabilities(snapshot, program_id, version_id, checked, "completed")
    compact = compact_version_capabilities(
        snapshot.review, snapshot.metadata, "sealed", content_hash(snapshot),
        program_id, version_id, checked, "completed",
    )

    assert compact == full
    assert compact["can_generate"] is True
    assert compact["unresolved_relations"] == 1


def test_compact_capabilities_block_an_unsealed_or_stale_version():
    program_id, version_id = uuid4(), uuid4()
    snapshot = SnapshotV2(review=ReviewState(
        review_id=uuid4(), import_job_id=uuid4(), analysis_version_id=version_id,
        lifecycle_policy="automatic_source_v2",
    ))
    checked = _checked(snapshot, version_id, program_id)
    checked["parameters"]["content_hash"] = digest("stale")

    result = compact_version_capabilities(
        snapshot.review, snapshot.metadata, "building", content_hash(snapshot),
        program_id, version_id, checked, "completed",
    )

    assert result["saved"] is False
    assert result["can_generate"] is False
    assert result["generation_reason"] == "Нет сохранённого графа автоматической версии"
