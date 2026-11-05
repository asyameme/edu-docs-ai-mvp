"""Перенос всех наблюдений V2 в версионную модель PostgreSQL."""

import json
from copy import deepcopy
from uuid import UUID, uuid4

import pytest
from document_cases import file_result, payload

from app.domain import SnapshotV2
from app.parser_adapter import to_snapshot


@pytest.mark.parametrize("kind", ["RPD", "RPP", "CURRICULUM", "MATRIX", "CKMV", "CHARACTERISTIC"])
def test_v2_snapshot_keeps_full_documents_and_valid_field_evidence(kind):
    data, pid, doc_id = payload(kind), uuid4(), uuid4()
    before = deepcopy(data)
    state, _ = to_snapshot(pid, [file_result(data, doc_id)])
    result = SnapshotV2.model_validate(state)
    assert data == before
    assert result.metadata["parsed_documents"][str(doc_id)] == data
    assert result.metadata["program_id"] == str(pid)
    assert result.metadata["document_program_codes"][str(doc_id)] == "26/5003/1"
    assert result.entities
    assert result.source_links
    for source in result.source_refs:
        assert source.document_id == doc_id
        assert json.loads(source.locator)["file"]
    for entity in result.entities:
        UUID(str(entity.id))


def test_v2_without_explicit_version_keeps_extra_fields_in_snapshot():
    data = payload()
    data["meta"].pop("schema_version")
    doc_id = uuid4()
    state, _ = to_snapshot(uuid4(), [file_result(data, doc_id)])
    result = SnapshotV2.model_validate(state)
    assert result.metadata["parsed_documents"][str(doc_id)] == data
    assert (
        next(e for e in result.entities if e.kind == "document_section").attributes["text"]
        == "Исходный текст"
    )


def test_matrix_preserves_explicit_zero_semesters_and_cell_sources():
    state, _ = to_snapshot(uuid4(), [file_result(payload("MATRIX"))])
    result = SnapshotV2.model_validate(state)
    edges = [r for r in result.relations if r.type == "FORMS_COMPETENCY"]
    assert {(r.attributes["semester"], r.attributes["matrix_value"]) for r in edges} == {
        (1, 0),
        (2, 1),
    }
    assert any(link.observed_value == {"value": 0} for link in result.source_links)
    assert {json.loads(ref.locator)["cell"] for ref in result.source_refs} == {"B2", "B3"}


def test_matrix_does_not_lose_cells_when_other_documents_have_matching_nodes():
    first, second = payload(), payload()
    for document in (first, second):
        document["competencies"] = [
            {"source_ref": {"file": "source.docx"}, "code": "УК-1", "formulation": "Компетенция"}
        ]
    files = [file_result(first), file_result(second), file_result(payload("MATRIX"))]
    state, _ = to_snapshot(uuid4(), files)
    edges = [r for r in state["relations"] if r["type"] == "FORMS_COMPETENCY"]
    assert len(edges) == 2
    assert {r["attributes"]["matrix_value"] for r in edges} == {0, 1}


def test_rpp_keeps_sections_stages_workload_and_scale_as_objects():
    state, _ = to_snapshot(uuid4(), [file_result(payload("RPP"))])
    objects = SnapshotV2.model_validate(state).entities
    assert {e.kind for e in objects} >= {
        "discipline",
        "semester_load",
        "document_section",
        "practice_stage",
        "score_scale",
    }
    load = next(e for e in objects if e.kind == "semester_load")
    assert load.attributes["hours_by_kind"] == {"практика": 108}
    assert load.attributes["control_forms"] == ["зачёт"]
    assert (
        next(e for e in objects if e.kind == "document_section").attributes["text"]
        == "Исходный текст"
    )


def test_every_object_source_has_explicit_evidence_before_sql_projection():
    state, _ = to_snapshot(uuid4(), [file_result(payload("RPP"))])
    result = SnapshotV2.model_validate(state)
    covered = {
        (link.target_kind, link.target_id, link.source_ref_id) for link in result.source_links
    }
    for kind, objects in (("entity", result.entities), ("relation", result.relations)):
        for item in objects:
            assert all((kind, item.id, ref) in covered for ref in item.source_ref_ids)


def test_conflicting_documents_stay_separate_with_stable_scoped_ids():
    first, second = payload(), payload()
    second["disciplines"][0]["hours_total"] = 144
    pid, doc1, doc2 = uuid4(), uuid4(), uuid4()
    files = [file_result(first, doc1), file_result(second, doc2)]
    state, _ = to_snapshot(pid, files)
    reordered, _ = to_snapshot(pid, list(reversed(files)))
    other, _ = to_snapshot(uuid4(), files)
    assert {e["id"] for e in state["entities"]} == {e["id"] for e in reordered["entities"]}
    assert not {e["id"] for e in state["entities"]} & {e["id"] for e in other["entities"]}
    disciplines = [e for e in state["entities"] if e["kind"] == "discipline"]
    assert {e["attributes"]["hours_total"] for e in disciplines} == {108, 144}
    assert len({e["id"] for e in disciplines}) == 2


def test_mixed_versions_keep_v1_fields_and_explicit_unknown_values():
    old = payload(version=1)
    old.pop("document_sections")
    old["disciplines"][0]["source_ref"].pop("paragraph_index")
    new = payload("RPP")
    new["practice_stages"][0]["semester"] = None
    first, second = uuid4(), uuid4()
    state, _ = to_snapshot(uuid4(), [file_result(old, first), file_result(new, second)])
    result = SnapshotV2.model_validate(state)
    assert result.metadata["parsed_documents"][str(first)] == old
    assert result.metadata["parsed_documents"][str(second)] == new
    assert any(
        link.field_path == "/attributes/semester" and link.observed_value == {"value": None}
        for link in result.source_links
    )


def test_curriculum_relations_resolve_semester_from_table_row():
    data = payload("CURRICULUM")
    row = data["curriculum_items"][0]
    row["source_ref"].update(table=1, row=2)
    second = deepcopy(row)
    second["semester"] = 2
    second["source_ref"]["row"] = 3
    data["curriculum_items"].append(second)
    data["competencies"] = [
        {"source_ref": row["source_ref"], "code": "УК-1", "formulation": "Компетенция"}
    ]
    data["relations"] = [
        {
            "source_ref": second["source_ref"],
            "src_entity": "curriculum_item",
            "src_key": "Дисциплина",
            "dst_entity": "competency",
            "dst_key": "УК-1",
            "kind": "формирует",
        }
    ]
    state, issues = to_snapshot(uuid4(), [file_result(data)])
    assert not issues
    result = SnapshotV2.model_validate(state)
    edge = result.relations[0]
    assert next(e for e in result.entities if e.id == edge.source_id).attributes["semester"] == 2
