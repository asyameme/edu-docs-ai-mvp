"""Тесты фиксов предупреждений парсера по результатам прогона 22.11.2026.

Покрыты четыре дефекта, найденных на реальном пакете:
- мульти-результатные строки в таблице результатов обучения;
- потеря форм аттестации при несовпадении меток траекторий;
- вводящее в заблуждение «не удалось прочитать название» для пустых ячеек;
- обрыв раздела оценочных средств на псевдозаголовках «Задание N».
"""

from backend.contracts.document_v2.models import SourceRefV2
from backend.worker.parser.assessment import (
    extract_assessment_tools,
    is_assessment_heading,
)
from backend.worker.parser.common import read_section
from backend.worker.parser.discipline import extract_control_forms
from backend.worker.parser.results import extract_learning_outcomes
from docx import Document


def _outcomes(text):
    return extract_learning_outcomes(
        text,
        SourceRefV2(file="x.docx", table=1, row=1, col=2),
    )


def _ambiguous(issues):
    return [
        issue for issue in issues if "Неоднозначная запись" in issue.message
    ]


def test_bracketed_codes_split_into_separate_outcomes():
    outcomes, issues = _outcomes(
        "Знает принципы планирования исследования (ПКП-УНК-03.02. З-2), "
        "оценки ресурсов и рисков (ПКП-УНК-03.02. З-3)."
    )
    assert [outcome.code for outcome in outcomes] == [
        "ПКП-УНК-03.02. З-2",
        "ПКП-УНК-03.02. З-3",
    ]
    assert outcomes[0].text == "Знает принципы планирования исследования"
    assert all(outcome.kind == "знать" for outcome in outcomes)
    assert not _ambiguous(issues)


def test_prefix_codes_split_into_separate_outcomes():
    outcomes, issues = _outcomes(
        "ПКП-МО-1.1 З-1 — знает алгоритмы обучения с учителем. "
        "ПКП-МО-1.1 У-1 — умеет применять библиотеки."
    )
    assert [outcome.code for outcome in outcomes] == [
        "ПКП-МО-1.1 З-1",
        "ПКП-МО-1.1 У-1",
    ]
    assert [outcome.kind for outcome in outcomes] == ["знать", "уметь"]
    assert outcomes[1].text == "умеет применять библиотеки"
    assert not _ambiguous(issues)


def test_grouped_codes_share_one_text():
    outcomes, _ = _outcomes(
        "ПКП-ППК-ДА2.3 З-1, ПКП-ППК-ДА2.3 З-2, ПКП-ППК-ДА2.3 З-3 — "
        "знает принципы формирования выборок"
    )
    assert [outcome.code for outcome in outcomes] == [
        "ПКП-ППК-ДА2.3 З-1",
        "ПКП-ППК-ДА2.3 З-2",
        "ПКП-ППК-ДА2.3 З-3",
    ]
    assert len({outcome.text for outcome in outcomes}) == 1


def test_short_code_after_full_code_gets_prefix():
    outcomes, _ = _outcomes(
        "ПКП-ППК-ДА2.1 У-1 — применяет методы очистки; "
        "У-2 — преобразует данные в форматы."
    )
    assert [outcome.code for outcome in outcomes] == [
        "ПКП-ППК-ДА2.1 У-1",
        "ПКП-ППК-ДА2.1 У-2",
    ]
    assert outcomes[1].text == "преобразует данные в форматы"


def test_kind_conflict_between_heading_and_code_reported():
    outcomes, issues = _outcomes(
        "владеет:\n"
        "навыками разрешения конфликтов (ПКП-УНК-02.01. У-6)"
    )
    assert outcomes[0].code == "ПКП-УНК-02.01. У-6"
    assert any(
        "Вид результата в заголовке и коде различается" in issue.message
        for issue in issues
    )


def test_unclosed_bracket_stays_ambiguous():
    _, issues = _outcomes(
        "методы создания и документирования примеров (ПКП-ППК-Р2.1. З-1;"
    )
    assert _ambiguous(issues)


def test_single_outcome_lines_unchanged():
    outcomes, issues = _outcomes(
        "знает:\nосновы командной работы (ПКП-УНК-02.01 З-1)"
    )
    assert [outcome.code for outcome in outcomes] == ["ПКП-УНК-02.01 З-1"]
    assert not _ambiguous(issues)


def _control_forms_document():
    """Таблица контроля с меткой траектории, отличной от фильтра.

    Воспроизводит С3_001872: в таблице трудоёмкости опечатка «Проофиль»,
    в таблице контроля — «Профиль», из-за чего фильтр варианта
    отбрасывал строки семестров.
    """
    document = Document()
    heading = document.add_paragraph("2.1 Организация учебных занятий")
    heading.style = document.styles["Heading 2"]
    table = document.add_table(rows=3, cols=2)
    table.rows[0].cells[0].text = "Формы текущего контроля успеваемости"
    table.rows[0].cells[1].text = "Виды промежуточной аттестации"
    table.rows[1].cells[0].text = "Основная траектория Профиль ТОП-ИТ ПРО"
    table.rows[1].cells[1].text = "очная"
    table.rows[2].cells[0].text = "Семестр 3"
    table.rows[2].cells[1].text = "зачёт, устно-письменная форма"
    return document


def test_control_forms_fall_back_without_variant_filter():
    document = _control_forms_document()
    forms, issues = extract_control_forms(
        document,
        "x.docx",
        variant_rows={(1, 3): ("основная траектория профиль топ-ит про", None)},
        workload_variant=("основная траектория проофиль топ-ит про", "очная"),
        semesters={3},
    )
    assert forms == ["зачет"]
    assert any(
        "без фильтра траектории" in issue.message and issue.severity == "info"
        for issue in issues
    )
    assert not any(
        "Не найдены формы" in issue.message for issue in issues
    )


def _assessment_document_with_empty_tool_cell():
    document = Document()
    document.add_paragraph(
        "3.1.4 Методические материалы для проведения текущего "
        "контроля успеваемости"
    )
    table = document.add_table(rows=2, cols=3)
    header = table.rows[0].cells
    header[0].text = "№"
    header[1].text = "Код индикатора и индикатор достижения компетенции"
    header[2].text = "Контрольно-измерительные материалы (КИМ)"
    row = table.rows[1].cells
    row[0].text = "1"
    row[1].text = "ОПК-1.1 Применяет фундаментальные знания"
    row[2].text = ""
    return document


def test_empty_tool_cell_reports_missing_not_failure():
    document = _assessment_document_with_empty_tool_cell()
    _, _, issues = extract_assessment_tools(
        document,
        "x.docx",
        indicators=[],
    )
    assert any(
        issue.message == "Оценочные средства: оценочное средство не указано"
        and issue.category == "empty_cell"
        for issue in issues
    )
    assert not any(
        "не удалось прочитать название" in issue.message for issue in issues
    )


def _assessment_document_with_pseudo_heading():
    document = Document()
    heading = document.add_paragraph(
        "3.1.4 Методические материалы для проведения текущего "
        "контроля успеваемости"
    )
    heading.style = document.styles["Heading 3"]
    task = document.add_paragraph("Задание 4")
    task.style = document.styles["Heading 1"]
    table = document.add_table(rows=2, cols=3)
    header = table.rows[0].cells
    header[0].text = "№"
    header[1].text = "Код индикатора и индикатор достижения компетенции"
    header[2].text = "Контрольно-измерительные материалы (КИМ)"
    row = table.rows[1].cells
    row[0].text = "1"
    row[1].text = "ОПК-1.1 Применяет фундаментальные знания"
    row[2].text = "Расчётные задания"
    closing = document.add_paragraph("3.1.5 Методические материалы для оценки")
    closing.style = document.styles["Heading 3"]
    return document


def test_pseudo_heading_does_not_cut_assessment_section():
    document = _assessment_document_with_pseudo_heading()
    blocks, issues = read_section(
        document,
        "x.docx",
        lambda paragraph: is_assessment_heading(paragraph, "RPD"),
        "оценочные средства",
        keep_numbered_items=True,
    )
    assert not issues
    tables = [
        block for block, _ in blocks
        if not hasattr(block, "text")
    ]
    assert len(tables) == 1
