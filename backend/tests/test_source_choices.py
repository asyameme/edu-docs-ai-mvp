"""Документный выбор сохраняет предыдущие изменения графа и исходные наблюдения."""

from datetime import UTC, datetime
from uuid import uuid4

import pytest
from document_cases import payload
from test_local_document_analysis import formulation_package
from test_source_graph import build

from app.domain import ReviewDecision
from app.errors import DomainError
from app.review_logic import apply_changes
from app.validation_bridge import project_snapshot_documents


def choice(snapshot, document_id, old="Формулировка 0", new="Выбранный текст"):
    return ReviewDecision(
        conflict_id=uuid4(),
        variant_id=uuid4(),
        check_job_id=snapshot.review.review_id,
        decided_at=datetime.now(UTC),
        author="Тест",
        changes=[
            {
                "op": "set_document_value",
                "document_id": document_id,
                "field_path": "/competencies/0/formulation",
                "expected_old_value": old,
                "value": new,
            }
        ],
    )


def apply_choice(program_id, documents, snapshot, selections):
    from app.source_graph import apply_document_selections

    return apply_document_selections(program_id, documents, snapshot, selections)


def chat_change(snapshot, target, field, value):
    change = {
        "op": "set_attribute",
        "target_kind": "entity",
        "target_id": str(target),
        "field_path": field,
        "value": value,
    }
    changed = apply_changes(snapshot, [change])
    decision = ReviewDecision(
        conflict_id=uuid4(),
        variant_id=uuid4(),
        changes=[change],
        check_job_id=snapshot.review.review_id,
        decided_at=datetime.now(UTC),
        author="Чат",
    )
    return changed.model_copy(
        update={
            "review": snapshot.review.model_copy(
                update={
                    "decisions": [*snapshot.review.decisions, decision],
                }
            ),
        }
    )


def test_chat_then_document_choice_keeps_both_changes_and_original_evidence():
    program_id, documents = uuid4(), formulation_package()
    original, _ = build(documents, program_id)
    discipline = next(item for item in original.entities if item.kind == "discipline")
    changed = chat_change(original, discipline.id, "/attributes/name", "После чата")

    selected = apply_choice(
        program_id,
        documents,
        changed,
        [choice(changed, documents[0]["document_id"])],
    )

    assert selected.review.graph_ready
    projected = project_snapshot_documents(documents, selected)
    assert projected[0]["parsed"]["disciplines"][0]["name"] == "После чата"
    assert projected[0]["parsed"]["competencies"][0]["formulation"] == "Выбранный текст"
    assert len(selected.review.decisions) == 2
    observed = [
        link.observed_value["value"]
        for link in selected.source_links
        if link.field_path == "/attributes/formulation"
    ]
    assert set(observed) == {"Формулировка 0", "Формулировка 1"}
    assert original.review.decisions == []


def test_document_choice_then_chat_then_another_choice_accumulates():
    program_id, documents = uuid4(), formulation_package()
    original, _ = build(documents, program_id)
    first = apply_choice(
        program_id,
        documents,
        original,
        [choice(original, documents[0]["document_id"])],
    )
    discipline = next(item for item in first.entities if item.kind == "discipline")
    second = chat_change(first, discipline.id, "/attributes/name", "После чата")
    final = apply_choice(
        program_id,
        documents,
        second,
        [
            choice(second, documents[1]["document_id"], "Формулировка 1", "Другой выбор"),
        ],
    )
    projected = project_snapshot_documents(documents, final)
    assert projected[0]["parsed"]["disciplines"][0]["name"] == "После чата"
    assert [d["parsed"]["competencies"][0]["formulation"] for d in projected] == [
        "Выбранный текст",
        "Другой выбор",
    ]
    assert len(final.review.decisions) == 3


def test_old_document_variant_cannot_overwrite_a_chat_edit_of_the_same_field():
    program_id, documents = uuid4(), formulation_package()
    original, _ = build(documents, program_id)
    target = next(item for item in original.entities if item.kind == "competency")
    changed = chat_change(original, target.id, "/attributes/formulation", "После чата")
    with pytest.raises(DomainError):
        apply_choice(
            program_id,
            documents,
            changed,
            [choice(changed, documents[0]["document_id"])],
        )


def test_user_can_change_previous_selection_without_reverting_other_edits():
    program_id, documents = uuid4(), formulation_package()
    original, _ = build(documents, program_id)
    selected = choice(original, documents[0]["document_id"])
    first = apply_choice(program_id, documents, original, [selected])
    discipline = next(item for item in first.entities if item.kind == "discipline")
    second = chat_change(first, discipline.id, "/attributes/name", "После чата")
    alternative = selected.model_copy(update={"variant_id": uuid4(), "changes": [
        {**selected.changes[0], "value": "Повторный выбор"},
    ]})
    final = apply_choice(program_id, documents, second, [alternative])
    projected = project_snapshot_documents(documents, final)[0]["parsed"]
    assert projected["competencies"][0]["formulation"] == "Повторный выбор"
    assert projected["disciplines"][0]["name"] == "После чата"
    assert len(final.review.decisions) == 2


def test_structural_link_choice_after_chat_rename_keeps_both_edits():
    program_id = uuid4()
    parsed = payload("RPD")
    parsed["competencies"] = [
        {"code": code, "formulation": code,
         "source_ref": {"file": "source.docx", "paragraph_index": i + 3}}
        for i, code in enumerate(["УК-1", "УК-2"])
    ]
    parsed["relations"] = [{"src_entity": "discipline", "src_key": "Дисциплина",
        "dst_entity": "competency", "dst_key": "УК-1", "kind": "формирует",
        "source_ref": parsed["disciplines"][0]["source_ref"]}]
    document_id = str(uuid4())
    documents = [{"document_id": document_id, "parsed": parsed}]
    original, _ = build(documents, program_id)
    discipline = next(item for item in original.entities if item.kind == "discipline")
    changed = chat_change(original, discipline.id, "/attributes/name", "Новое имя")
    selection = choice(changed, document_id).model_copy(update={"changes": [{
        "op": "set_competency_links", "document_id": document_id, "collection": "disciplines",
        "record_index": 0, "expected_codes": ["УК-1"], "codes": ["УК-2"],
    }]})
    selected = apply_choice(program_id, documents, changed, [selection])
    projected = project_snapshot_documents(documents, selected)[0]["parsed"]
    assert projected["disciplines"][0]["name"] == "Новое имя"
    assert projected["relations"][0]["src_key"] == "Новое имя"
    assert projected["relations"][0]["dst_key"] == "УК-2"
    assert not selected.metadata["unresolved_relations"]
    assert any(item.type == "FORMS_COMPETENCY" for item in selected.relations)


def test_code_choice_then_later_chat_and_choice_keep_reference_provenance():
    program_id = uuid4()
    parsed = payload("CKMV")
    document_id = str(uuid4())
    documents = [{"document_id": document_id, "parsed": parsed}]
    original, _ = build(documents, program_id)
    selection = choice(original, document_id).model_copy(update={"changes": [
        {"op": "set_document_value", "document_id": document_id,
         "field_path": path, "expected_old_value": "УК-1", "value": "УК-2"}
        for path in ["/competencies/0/code", "/role_competency_links/0/competency_code"]
    ]})
    first = apply_choice(program_id, documents, original, [selection])
    competency = next(item for item in first.entities if item.kind == "competency")
    second = chat_change(first, competency.id, "/attributes/formulation", "После чата")
    final = apply_choice(program_id, documents, second, [choice(
        second, document_id, "После чата", "Последний выбор")])
    projected = project_snapshot_documents(documents, final)[0]["parsed"]
    assert projected["competencies"][0]["code"] == "УК-2"
    assert projected["role_competency_links"][0]["competency_code"] == "УК-2"
    assert projected["competencies"][0]["formulation"] == "Последний выбор"
    assert any(item.observed_value.get("value") == "УК-1" for item in final.source_links)


@pytest.mark.parametrize("action, expected", [("keep", ["Общий текст"] * 2),
                                              ("split", ["Текст A", "Текст B"])])
def test_shared_indicator_choice_survives_later_chat_and_document_choice(action, expected):
    program_id, document_id = uuid4(), str(uuid4())
    parsed = payload("RPD")
    ref = {"file": "source.docx", "table": 1, "row": 2, "col": 3}
    parsed["competencies"] = [{"code": "УК-1", "formulation": "Описание",
                               "source_ref": {"file": "source.docx", "paragraph_index": 3}}]
    parsed["indicators"] = [{"code": "УК-1." + str(i + 1), "competency_code": "УК-1",
                              "text": "Общий текст", "source_ref": ref} for i in range(2)]
    documents = [{"document_id": document_id, "parsed": parsed}]
    original, _ = build(documents, program_id)
    selection = choice(original, document_id).model_copy(update={"changes": [
        {"op": "set_document_value", "document_id": document_id,
         "field_path": f"/indicators/{i}/text", "expected_old_value": "Общий текст", "value": value}
        for i, value in enumerate(["Текст A", "Текст B"])
    ] + [{"op": "set_shared_indicator_text", "document_id": document_id,
          "indicator_indices": [0, 1], "action": action}]})
    first = apply_choice(program_id, documents, original, [selection])
    target = next(item for item in first.entities if item.kind == "discipline")
    second = chat_change(first, target.id, "/attributes/name", "После чата")
    final = apply_choice(program_id, documents, second,
                         [choice(second, document_id, "Описание", "Новый текст")])
    projected = project_snapshot_documents(documents, final)[0]["parsed"]
    assert [item["text"] for item in projected["indicators"]] == expected
    assert projected["disciplines"][0]["name"] == "После чата"
    assert parsed["indicators"][0]["text"] == "Общий текст"


def test_rename_one_plan_semester_preserves_other_semester_links():
    from copy import deepcopy

    program_id, document_id = uuid4(), str(uuid4())
    parsed = payload("CURRICULUM")
    first = parsed["curriculum_items"][0]
    first["source_ref"] = {"file": "source.docx", "table": 1, "row": 1, "col": 2}
    second = deepcopy(first)
    second.update(semester=2, source_ref={**first["source_ref"], "row": 2})
    parsed["curriculum_items"].append(second)
    parsed["competencies"] = [{"code": "УК-1", "formulation": "Описание",
                               "source_ref": {"file": "source.docx", "paragraph_index": 3}}]
    parsed["relations"] = [{"src_entity": "curriculum_item",
                             "src_key": "Дисциплина::semester=" + str(i + 1),
                             "dst_entity": "competency", "dst_key": "УК-1", "kind": "формирует",
                             "source_ref": row["source_ref"]}
                            for i, row in enumerate([first, second])]
    documents = [{"document_id": document_id, "parsed": parsed}]
    original, _ = build(documents, program_id)
    target = next(item for item in original.entities if item.kind == "curriculum_item"
                  and item.attributes["semester"] == 1)
    chatted = chat_change(original, target.id, "/attributes/discipline_name", "Новое имя")
    final = apply_choice(program_id, documents, chatted, [])
    projected = project_snapshot_documents(documents, final)[0]["parsed"]
    assert [item["src_key"] for item in projected["relations"]] == [
        "Новое имя::semester=1", "Дисциплина::semester=2",
    ]
    assert not final.metadata["unresolved_relations"]
