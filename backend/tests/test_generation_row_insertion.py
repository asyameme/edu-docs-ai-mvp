"""Новая строка компетенции в РПД: вставка в DOCX и выбор набора по источникам."""

from io import BytesIO
from xml.etree import ElementTree as ET
from zipfile import ZipFile

import pytest
from test_generation_docx_patch import _paragraph, document_text, make_docx

from app.generation_bridge import _competency_row_content, _outcome_lines
from worker.generation import (
    DocxPatchError,
    RowInsertion,
    SourceDocx,
    generate_docx_package,
    insert_table_rows,
    read_area_text,
)

W_NS = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"


def _table(*rows):
    body = "".join(
        "<w:tr>" + "".join(f"<w:tc>{_paragraph(cell)}</w:tc>" for cell in row) + "</w:tr>"
        for row in rows
    )
    return f"<w:tbl>{body}</w:tbl>"

RPD = {"file": "РПД.docx", "section": "1.3", "table": 1}
KIM = {"file": "РПД.docx", "section": "3.1.4", "table": 2}


def _competency_table(*rows):
    return _table(*(("Код", "Результаты", "Индикаторы"), *rows))


# --- Вставка строк в DOCX ------------------------------------------------------


def test_insert_table_row_fills_cells_and_keeps_the_rest():
    docx = make_docx(
        _competency_table(
            ("УК-1. Способен А", "знает:", "УК-1.1. А (Базовый)"),
            ("УК-2. Способен Б", "знает:", "УК-2.1. Б (Средний)"),
        ),
    )

    patched = insert_table_rows(docx, [RowInsertion(
        {"table": 1, "row": 2},
        (
            ("УК-3. Способен В",),
            ("знает:", "• УК-3.1. З-1. Знает В."),
            ("УК-3.1. В (Базовый)",),
        ),
    )])

    assert read_area_text(patched, {"table": 1}) == (
        "Код", "Результаты", "Индикаторы",
        "УК-1. Способен А", "знает:", "УК-1.1. А (Базовый)",
        "УК-2. Способен Б", "знает:", "УК-2.1. Б (Средний)",
        "УК-3. Способен В", "знает:", "• УК-3.1. З-1. Знает В.",
        "УК-3.1. В (Базовый)",
    )
    # Другие части пакета не тронуты.
    with ZipFile(BytesIO(docx)) as before, ZipFile(BytesIO(patched)) as after:
        other = [n for n in before.namelist() if n != "word/document.xml"]
        for name in other:
            assert after.read(name) == before.read(name)


def test_insert_renumbers_a_numbering_column():
    docx = make_docx(
        _table(("№", "Индикатор"), ("1", "УК-1.1"), ("2", "УК-1.2")),
        _table(("№", "Индикатор"), ("1", "УК-2.1")),
    )

    patched = insert_table_rows(docx, [RowInsertion(
        {"table": 1, "row": 3}, (("0",), ("УК-1.3",)),
    )])

    assert read_area_text(patched, {"table": 1}) == (
        "№", "Индикатор", "1", "УК-1.1", "2", "УК-1.2", "3", "УК-1.3",
    )
    assert read_area_text(patched, {"table": 2}) == ("№", "Индикатор", "1", "УК-2.1")


def test_insert_numbers_continue_when_some_rows_have_empty_first_cells():
    # У части строк столбец № пуст (объединённые ячейки): общий пересчёт не
    # применяется, поэтому вставленная строка нумеруется от максимума.
    docx = make_docx(_table(
        ("№", "Индикатор"), ("1", "УК-1.1"), ("2", "УК-1.2"), ("", "склейка"), ("5", "УК-1.5"),
    ))

    patched = insert_table_rows(docx, [RowInsertion(
        {"table": 1, "row": 2}, (("0",), ("УК-1.6",)),
    )])

    assert read_area_text(patched, {"table": 1}) == (
        "№", "Индикатор", "1", "УК-1.1", "2", "УК-1.2", "", "склейка", "5", "УК-1.5",
        "6", "УК-1.6",
    )


def test_insert_leaves_the_number_cell_empty_without_any_numbers():
    docx = make_docx(_table(("Маркер", "Индикатор"), ("А", "УК-1.1")))

    patched = insert_table_rows(docx, [RowInsertion(
        {"table": 1, "row": 1}, (("0",), ("УК-1.2",)),
    )])

    assert read_area_text(patched, {"table": 1}) == (
        "Маркер", "Индикатор", "А", "УК-1.1", "", "УК-1.2",
    )


def test_inserted_rows_do_not_inherit_review_highlights():
    # Строка-шаблон — авторская пометка с жёлтой заливкой: новая строка —
    # обычный текст, заливка не наследуется.
    highlighted = (
        '<w:tbl><w:tr>'
        '<w:tc><w:p><w:r><w:rPr><w:highlight w:val="yellow"/></w:rPr>'
        "<w:t>УК-1. Способен А</w:t></w:r></w:p></w:tc>"
        '<w:tc><w:p><w:r><w:rPr><w:highlight w:val="yellow"/></w:rPr>'
        "<w:t>знает:</w:t></w:r></w:p></w:tc>"
        '<w:tc><w:p><w:r><w:rPr><w:highlight w:val="yellow"/></w:rPr>'
        "<w:t>УК-1.1. А</w:t></w:r></w:p></w:tc>"
        "</w:tr></w:tbl>"
    )
    docx = make_docx(highlighted)

    patched = insert_table_rows(docx, [RowInsertion(
        {"table": 1, "row": 1},
        (("УК-9. Способен Х",), ("знает:",), ("УК-9.1. Х",)),
    )])

    # Шаблонная строка остаётся с авторской заливкой, вставленная — без неё.
    patched_root = ET.fromstring(document_text(patched))
    inserted = next(
        row for row in patched_root.iter(f"{{{W_NS}}}tr")
        if "УК-9. Способен Х" in "".join(t.text or "" for t in row.iter(f"{{{W_NS}}}t"))
    )
    assert inserted.findall(f".//{{{W_NS}}}highlight") == []
    template = next(
        row for row in patched_root.iter(f"{{{W_NS}}}tr")
        if "УК-1. Способен А" in "".join(t.text or "" for t in row.iter(f"{{{W_NS}}}t"))
    )
    assert template.findall(f".//{{{W_NS}}}highlight")



def test_insert_checks_the_column_count():
    docx = make_docx(_competency_table(("УК-1", "знает:", "УК-1.1")))

    with pytest.raises(DocxPatchError, match="новая строка — 2"):
        insert_table_rows(docx, [RowInsertion(
            {"table": 1, "row": 1}, (("УК-2",), ("знает:",)),
        )])


# --- Рендер результатов --------------------------------------------------------


def test_outcome_lines_group_by_kind_and_normalise_codes():
    lines = _outcome_lines((
        {
            "kind": "знать",
            "code": "ПКП-8-ППК-ДА1.1. З-1",
            "text": "ПКП-8-ППК-ДА1.1. З-1. Знает теорию.",
        },
        {"kind": "уметь", "code": None, "text": "Умеет применять."},
        {"kind": "знать", "code": "УК-1.1. З-2", "text": "Знает методы."},
    ))

    assert lines == (
        "знает:",
        "• ПКП-ППК-ДА1.1. З-1. Знает теорию.",
        "• УК-1.1. З-2. Знает методы.",
        "умеет:",
        "• Умеет применять.",
    )
    assert _outcome_lines(()) == ("знает:", "умеет:")


# --- Выбор набора по источникам ------------------------------------------------

TARGET = "11111111-1111-4111-8111-111111111111"
OTHER_RPD = "22222222-2222-4222-8222-222222222222"
CKMV = "33333333-3333-4333-8333-333333333333"
ANYWHERE = "44444444-4444-4444-8444-444444444444"


def _meta(doc_type, name):
    return {"meta": {"doc_type": doc_type, "file_name": name}}


def _target_output():
    return {
        **_meta("RPD", "РПД Практикум.docx"),
        "disciplines": [{"name": "Практикум", "source_ref": {**RPD, "row": 2}}],
        "competencies": [
            {"code": "ОПК-2", "formulation": "Старая",
             "source_ref": {**RPD, "row": 2, "col": 1}},
        ],
        "indicators": [],
        "learning_outcomes": [],
        "assessment_tools": [{"name": "Практические работы"}],
        "relations": [
            {"kind": "проверяется", "src_entity": "indicator", "src_key": "ОПК-2.1",
             "dst_entity": "assessment_tool", "dst_key": "Практические работы",
             "source_ref": {**KIM, "row": 2}},
        ],
    }


def _parsed_documents(with_sibling=False, with_ckmv=False, with_anywhere=False):
    documents = {TARGET: _target_output()}
    if with_sibling:
        documents[OTHER_RPD] = {
            **_meta("RPD", "РПД Практикум Стандарт.docx"),
            "disciplines": [{"name": "Практикум", "source_ref": {**RPD, "row": 2}}],
            "competencies": [{"code": "УК-9", "formulation": "Способен Х"}],
            "indicators": [{"code": "УК-9.1", "text": "Делает Х (Базовый)"}],
            "learning_outcomes": [
                {"local_id": "LO-1", "code": "УК-9.1. З-1", "kind": "знать",
                 "text": "Знает Х."},
            ],
            "relations": [
                {"kind": "имеет", "src_key": "УК-9", "dst_key": "УК-9.1"},
                {"kind": "включает", "src_key": "УК-9", "dst_key": "LO-1"},
                {"kind": "проверяется", "src_key": "УК-9.1", "dst_key": "Тест"},
            ],
        }
    if with_ckmv:
        documents[CKMV] = {
            **_meta("CKMV", "цкмв.docx"),
            "competencies": [{"code": "УК-9", "formulation": "Способен Х по картотеке"}],
            "indicators": [{"code": "УК-9.1", "text": "Делает Х (Базовый)",
                            "competency_code": "УК-9"}],
            "learning_outcomes": [
                {"local_id": "LO-5", "code": "ПКП-8-ППК-ДА1.1. З-1", "kind": "знать",
                 "text": "ПКП-8-ППК-ДА1.1. З-1. Знает Х из картотеки."},
            ],
            "indicator_outcome_links": [
                {"indicator_code": "УК-9.1", "learning_outcome_id": "LO-5"},
            ],
        }
    if with_anywhere:
        documents[ANYWHERE] = {
            **_meta("RPD", "Другая.docx"),
            "disciplines": [{"name": "Другая", "source_ref": {**RPD, "row": 2}}],
            "competencies": [{"code": "УК-9", "formulation": "Способен Х"}],
            "indicators": [
                {"code": "УК-9.1", "text": "Делает Х (Базовый)", "competency_code": "УК-9"},
                {"code": "УК-9.2", "text": "Делает Х (Средний)", "competency_code": "УК-9"},
            ],
            "learning_outcomes": [],
            "relations": [],
        }
    return documents


def test_sibling_document_wins_over_ckmv():
    content = _competency_row_content(
        _parsed_documents(with_sibling=True, with_ckmv=True), _target_output(), TARGET, "УК-9"
    )

    assert content.source_label.startswith("одноимённой дисциплины")
    assert content.complete
    assert [i["code"] for i in content.indicators] == ["УК-9.1"]
    assert content.outcomes[0]["text"] == "Знает Х."
    assert content.tools == ("Тест",)


def test_ckmv_set_is_used_without_a_sibling():
    content = _competency_row_content(
        _parsed_documents(with_ckmv=True), _target_output(), TARGET, "УК-9"
    )

    assert content.source_label == "картотеки ЦКМВ"
    assert content.complete
    assert content.formulation == "Способен Х по картотеке"
    assert [i["code"] for i in content.indicators] == ["УК-9.1"]
    assert content.outcomes[0]["code"] == "ПКП-8-ППК-ДА1.1. З-1"


def test_program_union_keeps_indicators_and_marks_outcomes_manual():
    content = _competency_row_content(
        _parsed_documents(with_anywhere=True), _target_output(), TARGET, "УК-9"
    )

    assert not content.complete
    assert content.outcomes == ()
    assert [i["code"] for i in content.indicators] == ["УК-9.1", "УК-9.2"]
    # Формулировка добирается из любых документов программы.
    assert content.formulation == "Способен Х"


def test_no_sources_returns_none():
    assert _competency_row_content(
        _parsed_documents(), _target_output(), TARGET, "УК-9"
    ) is None


# --- Сквозная сборка: строка попадает в DOCX -----------------------------------


def test_added_competency_row_reaches_the_package():
    from app.generation_bridge import apply_competency_list_edits

    edit = type("Edit", (), {
        "document_id": TARGET, "collection": "disciplines", "record_index": 0,
        "record_name": "Практикум", "is_plan": False,
        "removed": (), "added": ("УК-9",),
    })()
    docx = make_docx(
        _competency_table(
            ("ОПК-2. Способен", "знает:", "ОПК-2.1. А"),
        ),
        _table(("№", "Индикатор", "КИМ"), ("1", "ОПК-2.1. А", "Практические работы")),
    )
    documents = [SourceDocx(
        document_id=TARGET, document_type="RPD", file_name="РПД Практикум.docx", content=docx,
    )]
    notes = []

    result = apply_competency_list_edits(
        documents, [edit], _parsed_documents(with_ckmv=True), notes
    )

    assert notes and notes[0].startswith(
        "РПД Практикум.docx (Практикум): добавлена компетенция УК-9"
    )
    package = generate_docx_package(result)
    with ZipFile(BytesIO(package)) as archive:
        content = archive.read("РПД Практикум.docx")
    texts = read_area_text(content, {"table": 1})
    assert "УК-9. Способен Х по картотеке" in texts
    assert "• ПКП-ППК-ДА1.1. З-1. Знает Х из картотеки." in texts
    assert "УК-9.1. Делает Х (Базовый)" in texts
    kim = read_area_text(content, {"table": 2})
    assert kim == (
        "№", "Индикатор", "КИМ",
        "1", "ОПК-2.1. А", "Практические работы",
        "2", "УК-9.1. Делает Х (Базовый)", "Практические работы",
    )
