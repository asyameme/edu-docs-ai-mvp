"""Поведение выбора и хэширования: исходные данные не изменяются и не теряются."""

import json
from copy import deepcopy
from pathlib import Path
from uuid import uuid4

import pytest
from pydantic import ValidationError

from app.domain import SnapshotV2
from app.errors import DomainError
from app.review_contract import (
    AnalysisV2,
    CheckRequest,
    CheckResult,
    Mutation,
    PrepareRequestV2,
)
from app.review_logic import (
    apply_changes,
    content_hash,
    copy_sources,
    map_analysis_v2_ids,
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


@pytest.mark.parametrize("value", [None, False, 0, 0.0, {"nested": [1, "ё", True]}])
def test_content_hash_preserves_legacy_encoding_and_input(value):
    from app.review_logic import digest

    state = sample().model_dump(mode="json")
    state["entities"][0]["attributes"]["extra"] = value
    original = deepcopy(state)
    legacy = deepcopy(state)
    legacy.pop("review", None)
    refs = {
        ref["id"]: {key: item for key, item in ref.items() if key != "id"}
        for ref in legacy["source_refs"]
    }
    for member in legacy["entities"] + legacy["relations"]:
        member["source_ref_ids"] = [refs[key] for key in member["source_ref_ids"]]
    legacy["source_refs"] = list(refs.values())
    legacy["source_links"] = [
        {**{key: item for key, item in link.items() if key not in {"id", "source_ref_id"}},
         "source": refs[link["source_ref_id"]]}
        for link in legacy["source_links"]
    ]
    assert content_hash(state) == digest(legacy)
    assert state == original


def test_journal_reuses_only_identical_parts_and_preserves_prior_values():
    from app.review_storage import journal_before_snapshot

    before = sample()
    changed_entity = before.entities[0].model_copy(update={"attributes": {"hours": 72}})
    after = before.model_copy(update={"entities": [changed_entity]})
    state = after.model_dump(mode="json")
    previous = journal_before_snapshot(before, after, state)
    assert previous == before.model_dump(mode="json")
    assert previous["entities"][0]["attributes"]["hours"] == 108
    assert state["entities"][0]["attributes"]["hours"] == 72
    assert previous["source_links"] is state["source_links"]


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
        ("prepare-request", PrepareRequestV2),
        ("analysis-response", AnalysisV2),
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


def test_v2_id_mapping_does_not_rewrite_values_or_document_identity():
    path = Path(__file__).parents[1] / "examples" / "review" / "analysis-response.json"
    data = json.loads(path.read_text(encoding="utf-8"))
    document = data["issues"][0]["source_document_ids"][0]
    data["issues"][0]["id"] = document
    parsed = AnalysisV2.model_validate(data)
    mapped = map_analysis_v2_ids(parsed, uuid4(), [{"document_id": document}])
    change = mapped["issues"][0]["variants"][1]["changes"][0]
    assert mapped["issues"][0]["id"] != document
    assert mapped["issues"][0]["source_document_ids"] == [document]
    assert change["document_id"] == document
    assert change["expected_old_value"] == 108
    assert change["value"] == 144


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
