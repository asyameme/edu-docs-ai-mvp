"""Поведение выбора и хэширования: исходные данные не изменяются и не теряются."""

import json
from copy import deepcopy
from pathlib import Path
from uuid import uuid4

import pytest
from pydantic import ValidationError

from app.domain import SnapshotV2
from app.errors import DomainError
from app.review_contract import AnalysisV1, CheckRequest, CheckResult, Mutation, PrepareRequest
from app.review_logic import (
    apply_changes,
    content_hash,
    copy_sources,
    map_analysis_ids,
    pointer_parts,
)


def sample():
    document, source, entity, link = [uuid4() for _ in range(4)]
    return SnapshotV2.model_validate(
        {
            "document_ids": [document],
            "entities": [
                {
                    "id": entity,
                    "kind": "discipline",
                    "attributes": {"hours": 108, "code": "001856"},
                    "source_ref_ids": [source],
                }
            ],
            "source_refs": [{"id": source, "document_id": document, "locator": "table:2;cell:B3"}],
            "source_links": [
                {
                    "id": link,
                    "target_kind": "entity",
                    "target_id": entity,
                    "field_path": "/attributes/hours",
                    "source_ref_id": source,
                    "observed_value": {"value": 108, "unit": "hour"},
                }
            ],
        }
    )


def change(state, value):
    return {
        "op": "set_attribute",
        "target_kind": "entity",
        "target_id": state.entities[0].id,
        "field_path": "/attributes/hours",
        "value": value,
    }


def test_copying_sources_changes_identity_but_preserves_checked_content():
    original = sample()
    saved = original.model_dump(mode="json")
    copied = copy_sources(original, uuid4())
    assert copied.entities[0].id == original.entities[0].id
    assert copied.source_refs[0].id != original.source_refs[0].id
    assert copied.source_links[0].source_ref_id == copied.source_refs[0].id
    assert content_hash(copied) == content_hash(original)
    assert original.model_dump(mode="json") == saved


def test_choice_keeps_raw_observation_and_does_not_mutate_previous_version():
    original = sample()
    selected = apply_changes(original, [change(original, 144)])
    assert selected.entities[0].attributes["hours"] == 144
    assert original.entities[0].attributes["hours"] == 108
    assert selected.source_links[0].observed_value == {"value": 108, "unit": "hour"}
    assert content_hash(selected) != content_hash(original)


def test_compatible_choices_are_idempotent_and_conflicting_choices_are_rejected():
    state = sample()
    selected = apply_changes(state, [change(state, 144), change(state, 144)])
    assert selected.entities[0].attributes["hours"] == 144
    with pytest.raises(DomainError, match="противоречат"):
        apply_changes(state, [change(state, 144), change(state, 206)])


def test_entity_removal_cannot_leave_a_dangling_relation():
    state = sample()
    raw = state.model_dump(mode="json")
    raw["relations"] = [
        {
            "id": str(uuid4()),
            "source_id": str(state.entities[0].id),
            "target_id": str(state.entities[0].id),
            "type": "example",
        }
    ]
    state = SnapshotV2.model_validate(raw)
    with pytest.raises(ValidationError):
        apply_changes(
            state,
            [{"op": "remove_entity", "target_kind": "entity", "target_id": state.entities[0].id}],
        )


@pytest.mark.parametrize("pointer", ["hours", "/attributes/~2invalid", "/attributes/~"])
def test_invalid_json_pointer_is_not_accepted(pointer):
    with pytest.raises(ValueError):
        pointer_parts(pointer)


def test_missing_rule_coverage_and_false_pass_with_issue_are_rejected():
    raw = {
        "ruleset": {
            "id": "example",
            "revision": "1",
            "config": {},
            "required_rules": ["hours"],
            "required_document_types": ["RPD"],
        },
        "coverage": [],
    }
    with pytest.raises(ValidationError):
        CheckResult.model_validate(raw)
    raw["coverage"] = [{"rule_code": "hours", "rule_revision": "1", "status": "passed"}]
    raw["issues"] = [
        {
            "id": str(uuid4()),
            "rule_code": "hours",
            "rule_revision": "1",
            "type": "example",
            "severity": "blocking",
            "message": "Example",
        }
    ]
    with pytest.raises(ValidationError):
        CheckResult.model_validate(raw)


def test_hash_preserves_unknown_zero_and_source_precision_as_distinct_values():
    original = sample()
    unknown = apply_changes(original, [change(original, None)])
    zero = apply_changes(original, [change(original, 0)])
    assert len({content_hash(original), content_hash(unknown), content_hash(zero)}) == 3
    altered = deepcopy(original.model_dump(mode="json"))
    altered["source_refs"][0]["locator"] = "table:2;cell:B4"
    assert content_hash(altered) != content_hash(original)


@pytest.mark.parametrize(
    "name,model",
    [
        ("prepare-request", PrepareRequest),
        ("analysis-response", AnalysisV1),
        ("check-request", CheckRequest),
        ("check-response", CheckResult),
    ],
)
def test_published_exchange_examples_match_the_executable_contract(name, model):
    path = Path(__file__).parents[1] / "examples" / "review" / (name + ".json")
    model.model_validate(json.loads(path.read_text(encoding="utf-8")))


def test_overlapping_field_choices_cannot_overwrite_a_previous_choice():
    state = sample()
    raw = state.model_dump(mode="json")
    raw["entities"][0]["attributes"]["group"] = {"hours": 108}
    state = SnapshotV2.model_validate(raw)
    changes = [change(state, {"hours": 144}), change(state, 206)]
    changes[0]["field_path"] = "/attributes/group"
    changes[1]["field_path"] = "/attributes/group/hours"
    with pytest.raises(DomainError, match="перекрывающиеся"):
        apply_changes(state, changes)


def test_id_mapping_does_not_rewrite_original_values_or_document_identity():
    path = Path(__file__).parents[1] / "examples" / "review" / "analysis-response.json"
    data = json.loads(path.read_text(encoding="utf-8"))
    old_entity = data["candidate"]["entities"][0]["id"]
    document = data["candidate"]["document_ids"][0]
    data["candidate"]["entities"][0]["id"] = document
    data["candidate"]["entities"][0]["attributes"]["external_id"] = document
    for link in data["candidate"]["source_links"]:
        if link["target_id"] == old_entity:
            link["target_id"] = document
    for issue in data["issues"]:
        for variant in issue["variants"]:
            for change_value in variant["changes"]:
                if change_value["target_id"] == old_entity:
                    change_value["target_id"] = document
    parsed = AnalysisV1.model_validate(data)
    mapped = map_analysis_ids(parsed, uuid4(), {"entities": [], "relations": []})
    assert mapped["candidate"]["document_ids"] == [document]
    assert mapped["candidate"]["source_refs"][0]["document_id"] == document
    assert mapped["candidate"]["entities"][0]["id"] != document
    assert mapped["candidate"]["entities"][0]["attributes"]["external_id"] == document
    assert (
        mapped["issues"][0]["variants"][0]["changes"][0]["target_id"]
        == mapped["candidate"]["entities"][0]["id"]
    )


def test_whole_object_operation_cannot_masquerade_as_an_unrelated_field_write():
    with pytest.raises(ValidationError):
        Mutation.model_validate(
            {
                "op": "remove_entity",
                "target_kind": "entity",
                "target_id": uuid4(),
                "field_path": "/attributes/unrelated",
            }
        )
