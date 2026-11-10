"""Проверки преобразования сохранённой версии во вход генератора."""

from uuid import uuid4

import pytest

from app.domain import SnapshotV2
from app.generation_bridge import GenerationBridgeError, approved_changes, snapshot_matrix_graph
from worker.generation import derive_matrix


def test_matrix_uses_saved_relation_and_attributes():
    discipline, duplicate, competency, relation, reference, document = [uuid4() for _ in range(6)]
    snapshot = SnapshotV2.model_validate(
        {
            "document_ids": [document],
            "entities": [
                {
                    "id": discipline,
                    "kind": "discipline",
                    "attributes": {"name": "Алгебра", "credit_units": 4},
                    "source_ref_ids": [reference],
                },
                {
                    "id": competency,
                    "kind": "competency",
                    "attributes": {"code": "УК-1"},
                    "source_ref_ids": [reference],
                },
                {
                    "id": duplicate,
                    "kind": "discipline",
                    "attributes": {"name": "Алгебра", "credit_units": 4},
                    "source_ref_ids": [reference],
                },
            ],
            "relations": [
                {
                    "id": relation,
                    "source_id": discipline,
                    "target_id": competency,
                    "type": "FORMS_COMPETENCY",
                    "source_ref_ids": [reference],
                }
            ],
            "source_refs": [
                {
                    "id": reference,
                    "document_id": document,
                    "locator": '{"file":"Алгебра.docx","row":3}',
                }
            ],
        }
    )
    matrix = derive_matrix(snapshot_matrix_graph(snapshot))
    assert matrix.competency_codes == ("УК-1",)
    assert len(matrix.rows) == 1
    assert matrix.rows[0].discipline == "Алгебра"
    assert matrix.rows[0].values == (1,)


def test_unsupported_structural_change_is_not_silently_ignored():
    review_id, import_id, version_id, conflict_id, variant_id, check_id, target_id = [
        uuid4() for _ in range(7)
    ]
    snapshot = SnapshotV2.model_validate(
        {
            "review": {
                "review_id": review_id,
                "import_job_id": import_id,
                "analysis_version_id": version_id,
                "decisions": [
                    {
                        "conflict_id": conflict_id,
                        "variant_id": variant_id,
                        "check_job_id": check_id,
                        "changes": [
                            {
                                "op": "remove_entity",
                                "target_kind": "entity",
                                "target_id": str(target_id),
                            }
                        ],
                        "decided_at": "2026-11-10T00:00:00Z",
                    }
                ],
                "confirmation": {
                    "checked_version_id": version_id,
                    "check_job_id": check_id,
                    "content_hash": "0" * 64,
                    "accepted_warning_ids": [],
                    "confirmed_at": "2026-11-10T00:00:00Z",
                },
            }
        }
    )
    with pytest.raises(GenerationBridgeError, match="структурные изменения"):
        approved_changes(snapshot)
