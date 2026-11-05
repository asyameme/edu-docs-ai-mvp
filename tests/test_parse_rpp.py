import tempfile
import unittest
from collections import Counter
from pathlib import Path

from docx import Document

from backend.worker.parser.assessment import extract_practice_score_scale
from backend.worker.parser.content import extract_course_content
from backend.worker.parser.discipline import (
    extract_practice_control_forms,
    extract_practice_workload,
)
from backend.contracts.parser_output import ParserOutput
from backend.worker.parser.parse_rpp import parse_rpp


ROOT = Path(__file__).resolve().parents[1]
RESULTS_TITLE = (
    "Перечень профессиональных компетенций, формирующих практическую "
    "составляющую результатов освоения программы:"
)


def practice_document(
    *,
    workload="total",
    control="🗹 зачет □ экзамен",
    parallel_scale=False,
    binary_scale=False,
    duplicate=False,
    results_number="1.7",
    assessment_code="УК-1.1",
):
    document = Document()
    for text in (
        "РАБОЧАЯ ПРОГРАММА",
        "П Р А К Т И К И",
        "Учебная практика",
        "Язык(и) обучения",
        "русский",
        "1.5. Требования подготовленности к прохождению практики",
        "Пререквизиты не требуются.",
        f"{results_number}. {RESULTS_TITLE}",
        "УК-1 Способен анализировать задачи",
    ):
        document.add_paragraph(text)
    if duplicate:
        document.add_paragraph("УК-1 Способен анализировать задачи")
    document.add_paragraph("ОПК-1 Способен применять знания")
    document.add_paragraph(f"{results_number}.2. {RESULTS_TITLE}")
    table = document.add_table(rows=2, cols=3)
    for cell, text in zip(table.rows[0].cells, (
        "Код и наименование компетенции",
        "Планируемые результаты обучения",
        "Код индикатора и индикатор достижения",
    )):
        cell.text = text
    for cell, text in zip(table.rows[1].cells, (
        "УК-1 Способен анализировать задачи",
        "Знать:\nОсновные понятия",
        "УК-1.1 Анализирует задачу",
    )):
        cell.text = text

    document.add_paragraph("2.1. Организация практики: модель с кратким описанием")
    if workload == "reference":
        document.add_paragraph("Виды и объемы работы указаны в актуальном учебном плане.")
    elif workload == "fractional":
        document.add_paragraph(
            "Общая трудоемкость практики составляет 3,5 зачетных единиц "
            "(126 академических часов)."
        )
        document.add_paragraph("Семестр 2: 3,5 з.е. (126 часов)")
    else:
        document.add_paragraph(
            "Общая трудоемкость практики составляет 3 зачетных единиц "
            "(108 академических часов)."
        )
        document.add_paragraph("Семестр 2: 3 з.е. (108 часов)")

    document.add_paragraph("2.2. Структура и содержание практики")
    table = document.add_table(rows=3, cols=3)
    for row, values in zip(table.rows, (
        ("Тема", "Вид занятий", "Часы"),
        ("Вводный инструктаж", "в присутствии преподавателя", "2"),
        ("Работа над проектом", "самостоятельная работа", "2-4"),
    )):
        for cell, text in zip(row.cells, values):
            cell.text = text

    document.add_paragraph("3.1.1. Виды и формы текущего контроля и промежуточной аттестации")
    document.add_paragraph("Промежуточная аттестация")
    document.add_paragraph(control)
    document.add_paragraph("3.1.3.2. Методика и критерии оценивания")
    if binary_scale:
        for text in (
            "Для оценки «зачтено» требуется выполнение всех следующих требований:",
            "Представлен отчет.",
            "Выполнено индивидуальное задание.",
            "При невыполнении одного из требований выставляется оценка «не зачтено».",
        ):
            document.add_paragraph(text)
    elif parallel_scale:
        table = document.add_table(rows=3, cols=3)
        for row, values in zip(table.rows, (
            ("Оценка", "Оценка", "Критерии"),
            ("отлично (A)", "зачтено", "Все задания выполнены"),
            ("неудовлетворительно (F)", "не зачтено", "Задания не выполнены"),
        )):
            for cell, text in zip(row.cells, values):
                cell.text = text
    else:
        table = document.add_table(rows=2, cols=3)
        for row, values in zip(table.rows, (
            ("Оценка СПбГУ", "Оценка ECTS", "Баллы"),
            ("зачтено", "A", "90-100"),
        )):
            for cell, text in zip(row.cells, values):
                cell.text = text

    document.add_paragraph(
        "3.1.3.3. Оценочные средства: контрольно-измерительные материалы "
        "и фонды оценочных средств"
    )
    table = document.add_table(rows=3, cols=3)
    for row, values in zip(table.rows, (
        ("№", "Код индикатора и индикатор достижения", "Контрольно-измерительные материалы"),
        ("", "1", "2"),
        ("1", assessment_code + " Анализирует задачу", "Отчет по практике"),
    )):
        for cell, text in zip(row.cells, values):
            cell.text = text
    document.add_paragraph("3.1.3.4. Рекомендуемая форма отчета")
    return document


class TestRppParser(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.original = parse_rpp(ROOT / "tests/originals/РПП.docx")

    def parse_document(self, document):
        with tempfile.TemporaryDirectory(prefix="rpp-test-") as directory:
            path = Path(directory) / "practice.docx"
            document.save(path)
            return parse_rpp(path)

    def test_original_load_forms_and_scale(self):
        output = self.original
        self.assertEqual(output.meta.doc_type, "RPP")
        self.assertEqual(output.meta.parser_config, "rpp_docx_v3")
        practice = output.disciplines[0]
        self.assertEqual((practice.credit_units, practice.hours_total), (18, 648))
        self.assertEqual(practice.semesters, [2, 3, 4, 5])
        self.assertEqual(practice.control_forms, ["зачет"])
        self.assertEqual(len(output.score_scale), 6)
        self.assertEqual(len(output.competencies), 33)
        self.assertIn("УК-1", {item.code for item in output.competencies})

    def test_corrupted_load_is_not_corrected(self):
        path = ROOT / "tests/stage_1/case_3/РПП.docx"
        output = parse_rpp(path)
        records, _ = extract_practice_workload(Document(path), path.name)
        periods = [item for item in records if item["scope"] == "semester"]
        self.assertEqual(output.disciplines[0].hours_total, 612)
        self.assertEqual(output.disciplines[0].credit_units, 18)
        self.assertEqual(sum(item["hours_total"] for item in periods), 648)
        self.assertEqual(sum(item["credit_units"] for item in periods), 16)
        self.assertTrue(any(
            issue.category == "manual_review" and "Семестр 4: 4" in issue.message
            for issue in output.parse_issues
        ))

    def test_round_trip_and_relation_integrity(self):
        output = self.original
        self.assertEqual(
            ParserOutput.model_validate_json(output.model_dump_json()), output,
        )
        keys = {
            "discipline": {item.name for item in output.disciplines},
            "competency": {item.code for item in output.competencies},
            "indicator": {item.code for item in output.indicators},
            "learning_outcome": {item.local_id for item in output.learning_outcomes},
            "assessment_tool": {item.name for item in output.assessment_tools},
        }
        for relation in output.relations:
            self.assertIn(relation.src_key, keys[relation.src_entity])
            self.assertIn(relation.dst_key, keys[relation.dst_entity])

    def test_rpd_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "РПП"):
            parse_rpp(ROOT / "tests/originals/РПД_матанализ1.docx")

    def test_curriculum_reference_does_not_invent_load_or_parent(self):
        output = self.parse_document(practice_document(workload="reference"))
        self.assertEqual(output.disciplines, [])
        self.assertEqual(output.course_content, [])
        self.assertTrue(any("отсылает к УП" in i.message for i in output.parse_issues))
        self.assertTrue(any(
            "Вводный инструктаж" in i.message and "не создана" in i.message
            for i in output.parse_issues
        ))

    def test_content_keeps_exact_hours_and_rejects_ranges(self):
        document = practice_document()
        topics, issues = extract_course_content(
            document, "practice.docx", doc_type="RPP",
        )
        self.assertEqual([(item.topic, item.hours) for item in topics], [
            ("Вводный инструктаж", 2),
        ])
        self.assertTrue(any(
            issue.category == "unexpected_format" and "2-4" in issue.message
            for issue in issues
        ))
        output = self.parse_document(document)
        self.assertEqual(len(output.course_content), 1)
        self.assertEqual(output.course_content[0].hours, 2)

    def test_checked_form_does_not_select_unchecked_exam(self):
        forms, issues = extract_practice_control_forms(
            practice_document(), "practice.docx",
        )
        self.assertEqual(forms, ["зачет"])
        self.assertEqual(issues, [])

    def test_unmarked_checkbox_is_not_guessed(self):
        for control in ("зачет □ экзамен", "незачет"):
            with self.subTest(control=control):
                forms, issues = extract_practice_control_forms(
                    practice_document(control=control), "practice.docx",
                )
                self.assertEqual(forms, [])
                self.assertTrue(issues)

    def test_parallel_grades_preserve_both_columns(self):
        items, issues = extract_practice_score_scale(
            practice_document(parallel_scale=True), "practice.docx",
        )
        self.assertEqual([item.grade_spbu for item in items], [
            "отлично (A)", "зачтено", "неудовлетворительно (F)", "не зачтено",
        ])
        self.assertEqual([item.grade_ects for item in items], ["A", None, "F", None])
        self.assertEqual([item.source_ref.col for item in items], [1, 2, 1, 2])
        self.assertFalse(any("таблица не сопоставлена" in i.message for i in issues))

    def test_binary_text_scale_preserves_all_and_any_conditions(self):
        items, _ = extract_practice_score_scale(
            practice_document(binary_scale=True), "practice.docx",
        )
        self.assertEqual({item.grade_spbu for item in items}, {"зачтено", "не зачтено"})
        for item in items:
            self.assertIn("всех", item.criterion)
            self.assertIn("одного", item.criterion)
            self.assertIn("Представлен отчет.", item.criterion)

    def test_assessment_numbering_row_is_not_a_tool(self):
        output = self.parse_document(practice_document())
        self.assertEqual([tool.name for tool in output.assessment_tools], [
            "Отчет по практике",
        ])
        self.assertTrue(any(
            relation.kind == "проверяется"
            and relation.src_key == "УК-1.1"
            and relation.dst_key == "Отчет по практике"
            for relation in output.relations
        ))

    def test_unknown_indicator_does_not_create_dangling_relation(self):
        output = self.parse_document(practice_document(assessment_code="УК-1.9"))
        self.assertFalse(any(
            relation.kind == "проверяется" for relation in output.relations
        ))
        self.assertTrue(any("УК-1.9" in issue.message for issue in output.parse_issues))

    def test_intro_table_echo_is_not_a_duplicate(self):
        output = self.parse_document(practice_document())
        self.assertEqual(Counter(item.code for item in output.competencies)["УК-1"], 1)
        duplicate = self.parse_document(practice_document(duplicate=True))
        self.assertEqual(Counter(item.code for item in duplicate.competencies)["УК-1"], 2)

    def test_section_numbers_can_change(self):
        output = self.parse_document(practice_document(results_number="4.9"))
        competency = next(item for item in output.competencies if item.code == "УК-1")
        self.assertTrue(competency.source_ref.section.startswith("4.9.2"))

    def test_text_only_competencies_are_an_allowed_format(self):
        document = practice_document()
        table = document.tables[0]._tbl
        table.getparent().remove(table)
        output = self.parse_document(document)
        self.assertEqual({item.code for item in output.competencies}, {"УК-1", "ОПК-1"})
        self.assertFalse(any(
            issue.category == "not_found"
            and issue.message == "Не найдена таблица компетенций"
            for issue in output.parse_issues
        ))

    def test_two_peer_results_sections_are_not_chosen_arbitrarily(self):
        document = practice_document()
        document.add_paragraph("4.9. " + RESULTS_TITLE)
        document.add_paragraph("ПК-99 Способен проверять результаты")
        output = self.parse_document(document)
        self.assertEqual(output.competencies, [])
        self.assertTrue(any(
            issue.category == "ambiguous"
            and "разделов результатов" in issue.message
            for issue in output.parse_issues
        ))

    def test_fractional_total_is_not_rounded_for_integer_model(self):
        output = self.parse_document(practice_document(workload="fractional"))
        self.assertEqual(output.disciplines, [])
        self.assertTrue(any(
            issue.category == "unexpected_format" and "3.5" in issue.message
            for issue in output.parse_issues
        ))


if __name__ == "__main__":
    unittest.main()
