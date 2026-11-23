"""Решения о списках компетенций и правки, которые не переносятся в документ."""

import json
import re
from io import BytesIO
from uuid import UUID
from zipfile import ZipFile

import pytest
from backend.contracts.document_v2.graph import EducationalGraph, GraphEdge, GraphNode
from backend.contracts.document_v2.models import SourceRefV2
from test_generation_docx_patch import _paragraph, document_text, make_docx

from app.domain import SnapshotV2
from app.generation_bridge import (
    apply_competency_list_edits,
    competency_list_edits,
    describe_unresolved,
)
from worker.agent.contracts import ChangeProposal, ProposedAttributeChange
from worker.agent.proposal_apply import plan_approved_changes, unwritable_changes
from worker.generation import (
    CONFLICT,
    DocxPatch,
    DocxPatchError,
    FieldChange,
    FieldEvidence,
    GenerationMappingError,
    OriginalDocument,
    RowRemoval,
    SourceDocx,
    SourceEvidence,
    UnresolvedPatch,
    build_generation_documents,
    generate_docx_package,
    generate_program_package,
    patch_docx_bytes,
    plan_patches,
    read_area_text,
    remove_table_rows,
    resolve_planned_patches,
)

RPD = {"file": "РПД.docx", "section": "1.3. Перечень результатов обучения", "table": 1}
PLAN = {"file": "Учебный план.docx", "section": "С06. Семестр 6", "table": 1}
PLAN_DOCUMENT = "5c5c0000-0000-4000-8000-000000000001"
RPD_DOCUMENT = "7d7d0000-0000-4000-8000-000000000001"
OTHER_PROGRAM_DOCUMENT = "7d7d0000-0000-4000-8000-000000000002"


def _table(*rows):
    body = "".join(
        "<w:tr>" + "".join(f"<w:tc>{_paragraph(cell)}</w:tc>" for cell in row) + "</w:tr>"
        for row in rows
    )
    return f"<w:tbl>{body}</w:tbl>"


def _other_parts(docx):
    with ZipFile(BytesIO(docx)) as archive:
        names = [name for name in archive.namelist() if "document.xml" not in name]
        return {name: archive.read(name) for name in names}


def _snapshot_with_decisions(*changes):
    return SnapshotV2.model_validate(
        {
            "review": {
                "review_id": "0b0b0000-0000-4000-8000-000000000001",
                "import_job_id": "0b0b0000-0000-4000-8000-000000000002",
                "analysis_version_id": "0b0b0000-0000-4000-8000-000000000003",
                "decisions": [
                    {
                        "conflict_id": "0b0b0000-0000-4000-8000-000000000004",
                        "variant_id": "0b0b0000-0000-4000-8000-000000000005",
                        "check_job_id": "0b0b0000-0000-4000-8000-000000000006",
                        "changes": list(changes),
                        "decided_at": "2026-11-23T00:00:00Z",
                    }
                ],
            }
        }
    )


def _links_change(document, collection, index, expected, codes):
    return {
        "op": "set_competency_links",
        "document_id": str(document),
        "collection": collection,
        "record_index": index,
        "expected_codes": list(expected),
        "codes": list(codes),
    }


def _formed(source, name, codes, plan=False):
    return [
        {
            "src_entity": "curriculum_item" if plan else "discipline",
            "src_key": name,
            "dst_entity": "competency",
            "dst_key": code,
            "kind": "формирует",
            "source_ref": dict(source),
        }
        for code in codes
    ]


def _source(document_id, document_type, file_name, content, patches=()):
    return SourceDocx(
        document_id=document_id,
        document_type=document_type,
        file_name=file_name,
        content=content,
        patches=tuple(patches),
    )


# --- Значение в соседней ячейке -------------------------------------------------


def test_value_in_neighbouring_cell_is_found_in_the_same_row():
    docx = make_docx(
        _table(
            ("ПКП-МО-1", "Способен проектировать системы"),
            ("ПКП-МО-2", "Способен внедрять системы"),
        )
    )
    ref = SourceEvidence("ref-1", "doc-1", json.dumps({"table": 1, "row": 1, "col": 1}))
    change = FieldChange("entity", "e-1", "/attributes/formulation", "Способен строить системы")
    link = FieldEvidence(
        "entity",
        "e-1",
        "/attributes/formulation",
        "ref-1",
        {"value": "Способен проектировать системы"},
    )

    planned = plan_patches([change], [link], [ref])
    resolved = resolve_planned_patches({"doc-1": docx}, planned)
    patched = patch_docx_bytes(docx, resolved["doc-1"])

    text = document_text(patched)
    assert "Способен строить системы" in text
    assert "ПКП-МО-1" in text and "Способен внедрять системы" in text
    assert _other_parts(patched) == _other_parts(docx)


def test_value_repeated_in_the_row_is_not_guessed():
    docx = make_docx(_table(("КОД", "Способен А", "Способен А")))
    ref = SourceEvidence("ref-1", "doc-1", json.dumps({"table": 1, "row": 1, "col": 1}))
    change = FieldChange("entity", "e-1", "/attributes/formulation", "Способен Б")
    link = FieldEvidence(
        "entity", "e-1", "/attributes/formulation", "ref-1", {"value": "Способен А"}
    )

    with pytest.raises(GenerationMappingError, match="не найдено записанное значение"):
        resolve_planned_patches({"doc-1": docx}, plan_patches([change], [link], [ref]))


def test_missing_value_can_be_reported_instead_of_failing_the_plan():
    docx = make_docx(_paragraph("Трудоёмкость: 72 ч"))
    ref = SourceEvidence("ref-1", "doc-1", json.dumps({"paragraph_index": 1}))
    found = FieldChange("entity", "e-1", "/attributes/hours", "108 ч")
    missing = FieldChange("entity", "e-2", "/attributes/hours_total", "144 ч")
    links = [
        FieldEvidence("entity", "e-1", "/attributes/hours", "ref-1", {"value": "72 ч"}),
        FieldEvidence("entity", "e-2", "/attributes/hours_total", "ref-1", {"value": "999 ч"}),
    ]
    planned = plan_patches([found, missing], links, [ref])
    unresolved = []

    resolved = resolve_planned_patches({"doc-1": docx}, planned, unresolved)

    assert [patch.new_text for patch in resolved["doc-1"]] == ["108 ч"]
    assert [(item.patch.new_text, item.occurrences) for item in unresolved] == [("144 ч", 0)]
    with pytest.raises(GenerationMappingError):
        resolve_planned_patches({"doc-1": docx}, planned)


def test_two_decisions_demanding_different_texts_for_one_place_are_both_reported():
    docx = make_docx(_paragraph("формализует задачу"))
    ref = SourceEvidence("ref-1", "doc-1", json.dumps({"paragraph_index": 1}))
    changes = [
        FieldChange("entity", "e-1", "/attributes/text", "Знает методы"),
        FieldChange("entity", "e-2", "/attributes/text", "Умеет соотносить"),
        FieldChange("entity", "e-3", "/attributes/text", "Знает методы"),
    ]
    links = [
        FieldEvidence(
            "entity", f"e-{index}", "/attributes/text", "ref-1", {"value": "формализует задачу"}
        )
        for index in (1, 2, 3)
    ]
    planned = plan_patches(changes, links, [ref])
    unresolved = []

    resolved = resolve_planned_patches({"doc-1": docx}, planned, unresolved)

    assert resolved["doc-1"] == ()
    assert [(item.patch.change.target_id, item.occurrences) for item in unresolved] == [
        ("e-1", CONFLICT),
        ("e-2", CONFLICT),
        ("e-3", CONFLICT),
    ]
    with pytest.raises(GenerationMappingError, match="претендуют на один фрагмент"):
        resolve_planned_patches({"doc-1": docx}, planned)


# --- Удаление строк таблицы -----------------------------------------------------


def test_row_removal_deletes_only_matching_rows_and_renumbers():
    docx = make_docx(
        _table(
            ("Код", "Результаты"),
            ("ПКП-МО-2. Способен А", "знает:"),
            ("ПКП-РГ-5. Способен Б", "знает:"),
        ),
        _table(
            ("№", "Индикатор", "КИМ"),
            ("1", "ПКП-МО-2.1. А", "Практика"),
            ("2", "ПКП-МО-2.2. А2", "Практика"),
            ("3", "ПКП-РГ-5.1. Б", "Вопросы"),
        ),
    )

    patched, kept = remove_table_rows(
        docx,
        [
            RowRemoval({"table": 1, "row": 3}, "ПКП-РГ-5"),
            RowRemoval({"table": 2, "row": 4}, "ПКП-РГ-5.1"),
            # Строка с другим кодом не трогается, даже если координата ведёт на неё.
            RowRemoval({"table": 2, "row": 2}, "ПКП-РГ-5"),
            RowRemoval({"table": 2, "row": 9}, "ПКП-РГ-5"),
        ],
    )

    assert "ПКП-РГ-5" not in document_text(patched)
    assert kept == (
        "таблица 2, строка 2: нет ячейки, начинающейся с «ПКП-РГ-5» "
        "(строка: «1 | ПКП-МО-2.1. А | Практика»)",
        "таблица 2, строка 9: такой строки нет в документе",
    )
    # Нумерация оставшихся строк снова без пропусков.
    assert [text for text in read_area_text(patched, {"table": 2}) if text] == [
        "№",
        "Индикатор",
        "КИМ",
        "1",
        "ПКП-МО-2.1. А",
        "Практика",
        "2",
        "ПКП-МО-2.2. А2",
        "Практика",
    ]
    assert _other_parts(patched) == _other_parts(docx)


def test_document_without_matching_rows_is_returned_untouched():
    docx = make_docx(
        '<w:tbl><w:tr><w:tc><w:tcPr><w:vMerge w:val="restart"/></w:tcPr>'
        f"{_paragraph('ПКП-РГ-5. Способен Б')}</w:tc></w:tr></w:tbl>"
    )

    patched, kept = remove_table_rows(
        docx,
        [
            RowRemoval({"table": 1, "row": 1}, "ПКП-РГ-5"),
            RowRemoval({"table": 1, "row": 1}, "ПКП-МО-2"),
        ],
    )

    assert patched is docx
    assert kept == (
        "таблица 1, строка 1: строка начинает объединённую по вертикали ячейку",
        "таблица 1, строка 1: нет ячейки, начинающейся с «ПКП-МО-2» "
        "(строка: «ПКП-РГ-5. Способен Б»)",
    )


# --- Учебный план: список компетенций в ячейке ----------------------------------


def _plan_parsed(formed, known=()):
    row_ref = {**PLAN, "row": 2, "col": 4}
    return {
        "curriculum_items": [
            {"discipline_name": "Моделирование", "semester": 6, "source_ref": row_ref}
        ],
        "competencies": [{"code": code, "source_ref": {**PLAN, "row": 9}} for code in known],
        "relations": _formed({**PLAN, "row": 2, "col": 3}, "Моделирование", formed, plan=True),
    }


def test_plan_codes_cell_is_rewritten_in_place():
    original = ("ОПК-1", "ОПК-2", "ПКА-1")
    final = ("ОПК-1", "ОПК-2", "ПКА-1", "ПКП-ППК-ДА1")
    parsed = _plan_parsed(original, known=final)
    docx = make_docx(
        _table(
            ("Индекс", "ЗЕТ", "Компетенции", "Дисциплина"),
            ("[001877] Моделирование", "3", ", ".join(original), "6"),
        )
    )
    snapshot = _snapshot_with_decisions(
        _links_change(PLAN_DOCUMENT, "curriculum_items", 0, original, final)
    )

    edits = competency_list_edits(snapshot, {PLAN_DOCUMENT: parsed})
    assert [(edit.added, edit.removed, edit.record_name) for edit in edits] == [
        (("ПКП-ППК-ДА1",), (), "Моделирование")
    ]
    notes = []
    documents = apply_competency_list_edits(
        [_source(PLAN_DOCUMENT, "CURRICULUM", "Учебный план.docx", docx)],
        edits,
        {PLAN_DOCUMENT: parsed},
        notes,
    )

    assert notes == []
    with ZipFile(BytesIO(generate_docx_package(documents))) as archive:
        result = archive.read("Учебный план.docx")
    assert "ОПК-1, ОПК-2, ПКА-1, ПКП-ППК-ДА1" in document_text(result)
    assert _other_parts(result) == _other_parts(docx)


def test_plan_codes_cell_that_cannot_be_found_is_reported_not_guessed():
    original = ("ОПК-1", "ОПК-2")
    docx = make_docx(_table(("Дисциплина", "Часы"), ("Моделирование", "108")))
    snapshot = _snapshot_with_decisions(
        _links_change(PLAN_DOCUMENT, "curriculum_items", 0, original, ("ОПК-2",))
    )
    parsed = {PLAN_DOCUMENT: _plan_parsed(original, known=original)}

    edits = competency_list_edits(snapshot, parsed)
    notes = []
    documents = apply_competency_list_edits(
        [_source(PLAN_DOCUMENT, "CURRICULUM", "Учебный план.docx", docx)],
        edits,
        parsed,
        notes,
    )

    assert documents[0].patches == ()
    assert notes == [
        "Учебный план.docx (Моделирование): ячейка со списком компетенций не найдена "
        "однозначно — измените список вручную (убрать: ОПК-1)"
    ]


def test_plan_choice_targets_only_one_semester():
    parsed = _plan_parsed(("ОПК-1", "ОПК-2"), known=("ОПК-1", "ОПК-2", "ОПК-3"))
    other = {**PLAN, "row": 3, "col": 3}
    parsed["curriculum_items"].append({
        "discipline_name": "Моделирование", "semester": 7, "source_ref": other,
    })
    parsed["relations"].extend(_formed(other, "Моделирование", ("ОПК-3",), plan=True))
    selected = _snapshot_with_decisions(
        _links_change(PLAN_DOCUMENT, "curriculum_items", 0, ("ОПК-1", "ОПК-2"), ("ОПК-2",))
    )

    [edit] = competency_list_edits(selected, {PLAN_DOCUMENT: parsed})

    assert edit.original == ("ОПК-1", "ОПК-2")
    assert edit.final == ("ОПК-2",)


# --- РПД: компетенция — строка таблицы ------------------------------------------


def _rpd_parsed(competencies):
    title = {"file": "РПД.docx", "section": "титул", "table": 3, "row": 1}
    indicators = [f"{code}.1" for code in competencies]
    results = [
        {
            "kind": "формирует",
            "src_entity": "discipline",
            "src_key": "Технологии MLOps",
            "dst_entity": "competency",
            "dst_key": code,
            "source_ref": {**RPD, "row": index + 2, "col": 1},
        }
        for index, code in enumerate(competencies)
    ]
    assessment = [
        {
            "kind": "проверяется",
            "src_entity": "indicator",
            "src_key": indicator,
            "dst_entity": "assessment_tool",
            "dst_key": "Практические работы",
            "source_ref": {"file": "РПД.docx", "table": 2, "row": index + 2, "section": "3.1.4"},
        }
        for index, indicator in enumerate(indicators)
    ]
    return {
        "disciplines": [{"name": "Технологии MLOps", "source_ref": title}],
        "competencies": [
            {
                "code": code,
                "formulation": f"Способен {index}",
                "source_ref": {**RPD, "row": index + 2, "col": 1},
            }
            for index, code in enumerate(competencies)
        ],
        "indicators": [
            {"code": indicator, "source_ref": {"file": "РПД.docx", "table": 2, "row": index + 2}}
            for index, indicator in enumerate(indicators)
        ],
        "relations": results + assessment,
    }


def _rpd_docx(competencies):
    results = [("Код", "Результаты")] + [
        (f"{code}. Способен {index}", "знает:") for index, code in enumerate(competencies)
    ]
    assessment = [("№", "Индикатор", "КИМ")] + [
        (str(index + 1), f"{code}.1. Индикатор", "Практические работы")
        for index, code in enumerate(competencies)
    ]
    return make_docx(_table(*results), _table(*assessment), _table(("Технологии MLOps",)))


def test_rpd_rows_of_removed_competency_are_deleted_and_renumbered():
    competencies = ("ПКП-МО-2", "ПКП-ППК-ДА4", "ПКП-РГ-5")
    docx = _rpd_docx(competencies)
    snapshot = _snapshot_with_decisions(
        _links_change(RPD_DOCUMENT, "disciplines", 0, competencies, ("ПКП-МО-2",))
    )

    edits = competency_list_edits(snapshot, {RPD_DOCUMENT: _rpd_parsed(competencies)})
    assert [edit.removed for edit in edits] == [("ПКП-ППК-ДА4", "ПКП-РГ-5")]
    notes = []
    documents = apply_competency_list_edits(
        [_source(RPD_DOCUMENT, "RPD", "РПД.docx", docx)],
        edits,
        {RPD_DOCUMENT: _rpd_parsed(competencies)},
        notes,
    )

    assert notes == []
    with ZipFile(BytesIO(generate_docx_package(documents))) as archive:
        result = archive.read("РПД.docx")
    text = document_text(result)
    assert "ПКП-ППК-ДА4" not in text and "ПКП-РГ-5" not in text and "ПКП-МО-2" in text
    assert [item for item in read_area_text(result, {"table": 2}) if item] == [
        "№",
        "Индикатор",
        "КИМ",
        "1",
        "ПКП-МО-2.1. Индикатор",
        "Практические работы",
    ]
    assert _other_parts(result) == _other_parts(docx)


def test_new_competency_in_rpd_is_left_for_a_manual_edit():
    snapshot = _snapshot_with_decisions(
        _links_change(
            RPD_DOCUMENT, "disciplines", 0, ("ПКП-МО-2",), ("ПКП-МО-2", "ПКП-РГ-5")
        )
    )
    # Новая компетенция известна программе по другому документу.
    parsed = {
        RPD_DOCUMENT: _rpd_parsed(("ПКП-МО-2",)),
        OTHER_PROGRAM_DOCUMENT: {
            "curriculum_items": [],
            "relations": [],
            "competencies": [{"code": "ПКП-РГ-5"}],
        },
    }
    edits = competency_list_edits(snapshot, parsed)
    notes = []

    documents = apply_competency_list_edits(
        [_source(RPD_DOCUMENT, "RPD", "РПД.docx", _rpd_docx(("ПКП-МО-2",)))],
        edits,
        parsed,
        notes,
    )

    assert documents[0].row_removals == ()
    assert notes == [
        "РПД.docx (Технологии MLOps): добавьте компетенцию ПКП-РГ-5 вручную — система "
        "не составляет результаты обучения и индикаторы для дисциплины"
    ]


def test_shared_assessment_row_is_not_deleted():
    kept, removed = "ПКП-МО-2", "ПКП-РГ-5"
    parsed = _rpd_parsed((kept, removed))
    links = [item for item in parsed["relations"] if item["kind"] == "проверяется"]
    links[1]["source_ref"] = links[0]["source_ref"]
    choice = _snapshot_with_decisions(
        _links_change(RPD_DOCUMENT, "disciplines", 0, (kept, removed), (kept,))
    )
    originals = {RPD_DOCUMENT: parsed}
    notes = []

    [document] = apply_competency_list_edits(
        [_source(RPD_DOCUMENT, "RPD", "РПД.docx", _rpd_docx((kept, removed)))],
        competency_list_edits(choice, originals),
        originals,
        notes,
    )

    assert [(r.source_ref["table"], r.source_ref["row"]) for r in document.row_removals] == [
        (1, 3)
    ]
    assert len(notes) == 1 and "таблица 2, строка 2" in notes[0]


# --- Чат: правку, которую не записать в документ, отклоняют до применения -------


ENTITY = "9a9a0000-0000-4000-8000-000000000001"
REFERENCE = "9a9a0000-0000-4000-8000-000000000002"
CHAT_DOCUMENT = "9a9a0000-0000-4000-8000-000000000003"


def _discipline_snapshot():
    return SnapshotV2.model_validate(
        {
            "document_ids": [CHAT_DOCUMENT],
            "entities": [
                {
                    "id": ENTITY,
                    "kind": "discipline",
                    "attributes": {"name": "Интернет вещей", "hours_total": 108},
                    "source_ref_ids": [REFERENCE],
                }
            ],
            "source_refs": [
                {
                    "id": REFERENCE,
                    "document_id": CHAT_DOCUMENT,
                    "locator": json.dumps({"paragraph_index": 1}),
                }
            ],
            "source_links": [
                {
                    "id": "9a9a0000-0000-4000-8000-000000000004",
                    "target_kind": "entity",
                    "target_id": ENTITY,
                    "field_path": "/attributes/hours_total",
                    "source_ref_id": REFERENCE,
                    "observed_value": {"value": 108},
                }
            ],
        }
    )


def _approved(snapshot, new_value):
    proposal = ChangeProposal(
        kind="change_proposal",
        summary="Выравниваем объём по учебному плану",
        changes=[
            ProposedAttributeChange(
                target_kind="entity",
                target_id=UUID(ENTITY),
                field_path="hours_total",
                expected_old_value=108,
                new_value=new_value,
                reason="в плане 144 часа",
                source_ref_ids=[UUID(REFERENCE)],
            )
        ],
    )
    return plan_approved_changes(snapshot, proposal)


def test_edit_of_a_value_missing_from_the_document_is_reported():
    snapshot = _discipline_snapshot()
    changes = _approved(snapshot, 144)

    assert unwritable_changes(
        snapshot, changes, {CHAT_DOCUMENT: make_docx(_paragraph("Итого 36"))}
    ) == ["общий объём часов: прежнее значение «108 ч» не найдено в документе"]
    assert unwritable_changes(snapshot, changes, {}) == [
        "общий объём часов: исходный документ недоступен"
    ]


def test_edit_of_a_value_present_in_the_document_passes():
    snapshot = _discipline_snapshot()

    problems = unwritable_changes(
        snapshot, _approved(snapshot, 144), {CHAT_DOCUMENT: make_docx(_paragraph("Всего: 108 ч"))}
    )

    assert problems == []


def test_edit_of_a_table_instead_of_one_value_is_reported_not_crashing():
    snapshot = _discipline_snapshot()

    problems = unwritable_changes(snapshot, _approved(snapshot, {"лекции": 40}), {})

    assert len(problems) == 1
    assert problems[0] == (
        "общий объём часов: это не одно значение (список, таблица или текст с "
        "переносами) — укажите отдельное поле"
    )


def test_unresolved_patch_is_described_for_the_report():
    snapshot = _discipline_snapshot()
    patch = plan_patches(
        [FieldChange("entity", ENTITY, "/attributes/hours_total", 144)],
        [
            FieldEvidence(
                "entity", ENTITY, "/attributes/hours_total", REFERENCE, {"value": 108, "unit": "ч"}
            )
        ],
        [SourceEvidence(REFERENCE, CHAT_DOCUMENT, snapshot.source_refs[0].locator)],
    )[CHAT_DOCUMENT][0]

    notes = describe_unresolved(
        snapshot,
        [_source(CHAT_DOCUMENT, "RPD", "С7-8_Интернет вещей.docx", b"")],
        [UnresolvedPatch(CHAT_DOCUMENT, patch, 0)],
    )

    assert notes == [
        "С7-8_Интернет вещей.docx (Интернет вещей): прежнее значение «108 ч» поля "
        "«общий объём часов» не найдено в документе; новое значение «144 ч» не "
        "перенесено — исправьте вручную"
    ]


def _graph_with_competency():
    graph = EducationalGraph()
    reference = SourceRefV2(file="Учебный план.docx")
    graph.add_node(
        GraphNode(
            id="p|discipline|X",
            entity_type="discipline",
            attributes={"name": "X"},
            source_refs=[],
        )
    )
    graph.add_node(
        GraphNode(
            id="p|competency|УК-1",
            entity_type="competency",
            attributes={"code": "УК-1"},
            source_refs=[],
        )
    )
    graph.add_edge(
        GraphEdge(
            source_id="p|discipline|X",
            target_id="p|competency|УК-1",
            relation_type="формирует",
            source_ref=reference,
            document_type="SNAPSHOT",
            value=1,
        )
    )
    return graph


def test_one_refused_replacement_does_not_stop_the_package():
    # Заменяемое число разрезано границей двух оформлений: однозначно оформить
    # новое число нельзя.
    mixed_formatting = (
        '<w:p><w:r><w:rPr><w:b/></w:rPr><w:t>Трудоёмкость: 7</w:t></w:r>'
        "<w:r><w:t>2 ч</w:t></w:r></w:p>"
    )
    documents = [
        _source(
            "doc-1",
            "RPD",
            "РПД.docx",
            make_docx(mixed_formatting),
            [DocxPatch({"paragraph_index": 1}, "72 ч", "108 ч")],
        )
    ]
    notes, applied = [], []

    package = generate_program_package(
        documents, _graph_with_competency(), notes=notes, applied=applied
    )

    assert applied == []
    with ZipFile(BytesIO(package)) as archive:
        assert archive.read("РПД.docx") == documents[0].content
        assert "не заменено" in archive.read("Отчёт о сборке.txt").decode("utf-8-sig")
    assert len(notes) == 1
    assert notes[0].startswith("РПД.docx: не заменено «72 ч» на «108 ч»")
    assert "разным форматированием" in notes[0]
    with pytest.raises(DocxPatchError, match="разным форматированием"):
        generate_program_package(documents, _graph_with_competency())


def _runs(*parts):
    """Абзац из фрагментов: для каждого — XML свойств и текст."""
    body = "".join(
        f'<w:r>{properties}<w:t xml:space="preserve">{text}</w:t></w:r>'
        for properties, text in parts
    )
    return f"<w:p>{body}</w:p>"


def test_language_tag_is_not_treated_as_different_formatting():
    docx = make_docx(
        _runs(
            ('<w:rPr><w:sz w:val="20"/><w:lang w:val="ru-RU"/></w:rPr>', "Текст. "),
            ('<w:rPr><w:sz w:val="20"/></w:rPr>', "(Средний)"),
        )
    )

    patched = patch_docx_bytes(docx, [DocxPatch({}, "Текст. (Средний)", "Текст.")])

    assert read_area_text(patched, {}) == ("Текст.",)


def test_local_edit_keeps_the_formatting_of_untouched_fragments():
    # Пометка уровня выделена курсивом: удаляется она, остальной текст не трогается.
    italic = make_docx(_runs(("", "Текст. "), ("<w:rPr><w:i/></w:rPr>", "(Средний)")))
    patched = patch_docx_bytes(italic, [DocxPatch({}, "Текст. (Средний)", "Текст.")])
    assert read_area_text(patched, {}) == ("Текст.",)
    assert re.search(r"<w:i\s*/>", document_text(patched))

    # Число выделено полужирным: после замены выделение остаётся у числа.
    bold = make_docx(_runs(("<w:rPr><w:b/></w:rPr>", "72 "), ("", "ч")))
    patched = patch_docx_bytes(bold, [DocxPatch({}, "72 ч", "108 ч")])
    assert read_area_text(patched, {}) == ("108 ч",)
    assert re.search(
        r"<w:r><w:rPr><w:b\s*/></w:rPr><w:t xml:space=\"preserve\">108 </w:t></w:r>",
        document_text(patched),
    )

    # Число разрезано границей двух оформлений: оформить новое однозначно нельзя.
    split = make_docx(_runs(("<w:rPr><w:b/></w:rPr>", "7"), ("", "2 ч")))
    with pytest.raises(DocxPatchError, match="разным форматированием"):
        patch_docx_bytes(split, [DocxPatch({}, "72 ч", "108 ч")])


def test_build_generation_documents_reports_missing_value_when_asked():
    docx = make_docx(_paragraph("Трудоёмкость: 72 ч"))
    ref = SourceEvidence("ref-1", "doc-1", json.dumps({"paragraph_index": 1}))
    stale = FieldEvidence("entity", "e-1", "/attributes/hours", "ref-1", {"value": 999})
    unresolved = []

    documents = build_generation_documents(
        [FieldChange("entity", "e-1", "/attributes/hours", 108)],
        [stale],
        [ref],
        [OriginalDocument("doc-1", "RPD", "РПД.docx", docx)],
        unresolved,
    )

    assert documents[0].patches == ()
    assert [(item.document_id, item.occurrences) for item in unresolved]
