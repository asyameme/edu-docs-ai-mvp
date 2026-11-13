"""Чтение версий, происхождение данных и решения четвёртого этапа."""

from datetime import UTC, datetime
from uuid import uuid4

import pytest
from pydantic import ValidationError

from app.domain import SnapshotV2, VersionCreate, VersionRead


def test_version_read_preserves_v2_matrix_value_and_field_evidence():
    entity_id, relation_id, ref_id, document_id, link_id = [uuid4() for _ in range(5)]
    version = VersionRead.model_validate(
        {
            "id": uuid4(),
            "program_id": uuid4(),
            "number": 2,
            "parent_version_id": uuid4(),
            "restored_from_version_id": None,
            "origin": "save",
            "created_at": datetime.now(UTC),
            "snapshot": {
                "schema_version": 2,
                "entities": [
                    {"id": entity_id, "kind": "discipline", "attributes": {"code": "001856"}}
                ],
                "relations": [
                    {
                        "id": relation_id,
                        "source_id": entity_id,
                        "target_id": entity_id,
                        "type": "FORMS_COMPETENCY",
                        "attributes": {"matrix_value": 0},
                    }
                ],
                "document_ids": [document_id],
                "source_refs": [
                    {"id": ref_id, "document_id": document_id, "locator": "sheet:1;cell:B2"}
                ],
                "source_links": [
                    {
                        "id": link_id,
                        "target_kind": "relation",
                        "target_id": relation_id,
                        "field_path": "/attributes/matrix_value",
                        "source_ref_id": ref_id,
                        "observed_value": {"value": 0},
                    }
                ],
            },
        }
    )
    assert version.snapshot.schema_version == 2
    assert version.snapshot.relations[0].attributes["matrix_value"] == 0
    assert version.snapshot.entities[0].attributes["code"] == "001856"
    assert version.snapshot.source_links[0].source_ref_id == ref_id


@pytest.mark.parametrize("value", [True, 2, -1, "1"])
def test_matrix_observation_does_not_accept_wrong_typed_values(value):
    identity = uuid4()
    with pytest.raises(ValidationError):
        SnapshotV2.model_validate(
            {
                "entities": [{"id": identity, "kind": "discipline"}],
                "relations": [
                    {
                        "id": uuid4(),
                        "source_id": identity,
                        "target_id": identity,
                        "type": "FORMS_COMPETENCY",
                        "attributes": {"matrix_value": value},
                    }
                ],
            }
        )


@pytest.mark.parametrize("value", [None, 0, 1])
def test_matrix_preserves_unknown_zero_and_one(value):
    identity = uuid4()
    snapshot = SnapshotV2.model_validate(
        {
            "entities": [{"id": identity, "kind": "discipline"}],
            "relations": [
                {
                    "id": uuid4(),
                    "source_id": identity,
                    "target_id": identity,
                    "type": "FORMS_COMPETENCY",
                    "attributes": {"matrix_value": value},
                }
            ],
        }
    )
    assert snapshot.relations[0].attributes["matrix_value"] == value


def test_matrix_value_is_only_allowed_for_competency_coverage():
    identity = uuid4()
    with pytest.raises(ValidationError):
        SnapshotV2.model_validate(
            {
                "entities": [{"id": identity, "kind": "discipline"}],
                "relations": [
                    {
                        "id": uuid4(),
                        "source_id": identity,
                        "target_id": identity,
                        "type": "HAS_TOPIC",
                        "attributes": {"matrix_value": 0},
                    }
                ],
            }
        )


@pytest.mark.parametrize("broken", ["target", "source", "field", "duplicate"])
def test_field_evidence_cannot_reference_absent_objects_or_duplicate_locations(broken):
    identity, source, document = uuid4(), uuid4(), uuid4()
    link = {
        "id": uuid4(),
        "target_kind": "entity",
        "target_id": identity,
        "field_path": "/attributes/hours",
        "source_ref_id": source,
        "observed_value": {"value": None},
    }
    if broken == "target":
        link["target_id"] = uuid4()
    if broken == "source":
        link["source_ref_id"] = uuid4()
    if broken == "field":
        link["field_path"] = "/attributes/nonexistent"
    links = [link]
    if broken == "duplicate":
        links.append({**link, "id": uuid4()})
    with pytest.raises(ValidationError):
        SnapshotV2.model_validate(
            {
                "entities": [{"id": identity, "kind": "discipline", "attributes": {"hours": None}}],
                "document_ids": [document],
                "source_refs": [
                    {"id": source, "document_id": document, "locator": "table:2;row:4"}
                ],
                "source_links": links,
            }
        )


def test_ordinary_save_does_not_allow_review_or_confirmation_from_the_client():
    with pytest.raises(ValidationError):
        VersionCreate.model_validate(
            {
                "expected_version_id": uuid4(),
                "signature": "Тестовый пользователь",
                "snapshot": {"schema_version": 2, "review": {
                    "review_id": uuid4(), "import_job_id": uuid4(),
                    "analysis_version_id": uuid4(),
                }},
            }
        )


def test_v1_saved_version_is_not_a_supported_read_contract():
    with pytest.raises(ValidationError):
        VersionRead.model_validate(
            {
                "id": uuid4(), "program_id": uuid4(), "number": 1,
                "parent_version_id": None, "restored_from_version_id": None,
                "origin": "initial", "created_at": datetime.now(UTC),
                "snapshot": {"schema_version": 1},
            }
        )
