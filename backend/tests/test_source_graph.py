"""Первый граф сохраняет исходные наблюдения и явно неразрешённые связи."""

from copy import deepcopy
from uuid import UUID, uuid4

import pytest
from document_cases import payload
from test_local_document_analysis import formulation_package

from app.validation_bridge import project_snapshot_documents


def build(documents, program_id=None):
    from app.source_graph import source_snapshot

    return source_snapshot(
        program_id or uuid4(),
        documents,
        review_id=uuid4(),
        import_job_id=uuid4(),
        version_id=uuid4(),
    )


def test_conflicting_formulations_survive_without_choosing_a_variant():
    documents = formulation_package()
    original = deepcopy(documents)

    snapshot, issues = build(documents)

    assert snapshot.review.graph_ready
    assert snapshot.review.lifecycle_policy == "automatic_source_v2"
    assert snapshot.review.decisions == []
    assert snapshot.review.confirmation is None
    assert not issues
    assert {
        entity.attributes["formulation"]
        for entity in snapshot.entities
        if entity.kind == "competency"
    } == {"Формулировка 0", "Формулировка 1"}
    assert project_snapshot_documents(documents, snapshot) == documents
    assert documents == original


@pytest.mark.parametrize("ambiguous", [False, True])
def test_unresolved_observation_keeps_source_and_candidates_without_an_edge(ambiguous):
    parsed = payload("CKMV" if ambiguous else "RPD")
    ref = {"file": "source.docx", "paragraph_index": 9}
    if ambiguous:
        parsed["role_competency_links"][0].update(
            competency_code="УК-X",
            source_ref=ref,
        )
        parsed["competencies"] = [
            {
                "code": "УК-X",
                "formulation": f"Вариант {index}",
                "source_ref": {"file": "source.docx", "paragraph_index": index + 2},
            }
            for index in range(2)
        ]
    else:
        parsed["relations"] = [
            {
                "src_entity": "discipline",
                "src_key": "Дисциплина",
                "dst_entity": "competency",
                "dst_key": "УК-X",
                "kind": "формирует",
                "source_ref": ref,
            }
        ]
    document_id = str(uuid4())
    documents = [{"document_id": document_id, "parsed": parsed}]
    original = deepcopy(documents)

    snapshot, issues = build(documents)

    assert snapshot.review.graph_ready
    assert len(issues) == 1 and issues[0]["category"] == "unresolved_relation"
    assert not any(
        relation.type in {"FORMS_COMPETENCY", "ROLE_COMPETENCY"} for relation in snapshot.relations
    )
    unresolved = snapshot.metadata["unresolved_relations"]
    assert len(unresolved) == 1
    assert unresolved[0]["source_ref"]["paragraph_index"] == 9
    assert unresolved[0]["document_id"] == document_id
    candidates = unresolved[0]["resolution"]["target_candidates"]
    assert len(candidates) == (2 if ambiguous else 0)
    assert set(candidates).issubset({str(entity.id) for entity in snapshot.entities})
    assert documents == original


@pytest.mark.parametrize("kind", ["RPD", "RPP", "CURRICULUM", "CKMV", "CHARACTERISTIC", "MATRIX"])
def test_source_graph_preserves_each_supported_document_type(kind):
    documents = [{"document_id": str(uuid4()), "parsed": payload(kind)}]
    snapshot, _ = build(documents)
    assert snapshot.document_ids == [UUID(documents[0]["document_id"])]
    assert snapshot.entities
    assert project_snapshot_documents(documents, snapshot) == documents


def test_same_program_and_documents_keep_stable_public_graph_ids():
    documents, program_id = formulation_package(), uuid4()
    first, _ = build(documents, program_id)
    second, _ = build(documents, program_id)
    assert first.entities == second.entities
    assert first.relations == second.relations
    assert first.source_refs == second.source_refs
    assert first.source_links == second.source_links
