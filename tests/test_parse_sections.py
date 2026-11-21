import unittest
from pathlib import Path

from docx import Document
from docx.enum.style import WD_STYLE_TYPE
from docx.oxml import OxmlElement
from docx.oxml.ns import qn

from backend.contracts.document_v2.models import RPDOutput, RPPOutput
from backend.worker.parser.parse_rpd import parse_rpd
from backend.worker.parser.parse_rpp import parse_rpp
from backend.worker.parser.sections import extract_document_sections


ROOT = Path(__file__).resolve().parents[1]


class TestDocumentSections(unittest.TestCase):
    def test_text_tables_and_subheadings_keep_source_order(self):
        document = Document()
        document.add_paragraph("Титул")
        document.add_paragraph("2.2. Структура и содержание учебных занятий")
        document.add_paragraph("Тема:  деревья и графы.\n12–16 академических часов")
        document.add_paragraph("2.2.1. Задания к теме")
        document.add_paragraph("1. Найдите путь между вершинами.")
        table = document.add_table(rows=2, cols=2)
        for row, values in zip(table.rows, (
            ("Тема", "Часы"),
            ("Графы", "12–16"),
        )):
            for cell, text in zip(row.cells, values):
                cell.text = text
        document.add_paragraph("После таблицы: объясните решение.")
        document.add_paragraph("2.3. Следующий раздел")
        document.add_paragraph("Этот текст не относится к содержанию.")

        sections, issues = extract_document_sections(document, "course.docx")

        self.assertEqual(issues, [])
        self.assertEqual(len(sections), 1)
        section = sections[0]
        self.assertEqual(section.section_number, "2.2")
        self.assertEqual(section.title, "Структура и содержание учебных занятий")
        self.assertEqual(section.parent_section, "2")
        self.assertEqual(section.source_ref.model_dump(exclude_none=True), {
            "file": "course.docx",
            "section": "2.2. Структура и содержание учебных занятий",
            "paragraph_index": 2,
        })
        self.assertEqual(section.text, (
            "Тема:  деревья и графы.\n12–16 академических часов\n\n"
            "2.2.1. Задания к теме\n\n"
            "1. Найдите путь между вершинами.\n\n"
            "Тема\tЧасы\nГрафы\t12–16\n\n"
            "После таблицы: объясните решение."
        ))

    def test_merged_cells_are_not_repeated_and_real_repeats_are_kept(self):
        document = Document()
        document.add_paragraph("2.2. Структура и содержание дисциплины")
        table = document.add_table(rows=3, cols=3)
        table.cell(0, 0).merge(table.cell(0, 1)).text = "Общая шапка"
        table.cell(0, 2).text = "Часы"
        table.cell(1, 0).merge(table.cell(2, 0)).text = "Общая тема"
        table.cell(1, 1).text = "Лекции"
        table.cell(1, 2).text = "8"
        table.cell(2, 1).text = "Практика"
        table.cell(2, 2).text = "8"
        nested = table.cell(2, 1).add_table(rows=1, cols=1)
        nested.cell(0, 0).text = "Вложенная таблица"

        sections, issues = extract_document_sections(document, "tables.docx")

        self.assertEqual(issues, [])
        text = sections[0].text
        self.assertEqual(text.count("Общая шапка"), 1)
        self.assertEqual(text.count("Общая тема"), 1)
        self.assertEqual(text.count("8"), 2)
        self.assertEqual(text.count("Вложенная таблица"), 1)
        self.assertLess(text.index("Лекции"), text.index("Практика"))
        self.assertLess(text.index("Практика"), text.index("Вложенная таблица"))

    def test_toc_is_skipped_and_source_counts_table_paragraphs(self):
        document = Document()
        document.styles.add_style("TOC 1", WD_STYLE_TYPE.PARAGRAPH)
        document.add_paragraph("2.2. Структура и содержание учебных занятий", "TOC 1")
        document.add_table(rows=1, cols=1).cell(0, 0).text = "Титульная таблица"
        document.add_paragraph("См. раздел 2.2. Структура и содержание учебных занятий")
        document.add_paragraph("7.3. Структура и содержание учебных занятий")
        document.add_paragraph("Фактическое содержание")
        document.add_paragraph("7.4. Другой раздел")
        document.add_paragraph("Чужой текст")

        sections, issues = extract_document_sections(document, "renumbered.docx")

        self.assertEqual(issues, [])
        self.assertEqual(len(sections), 1)
        self.assertEqual(sections[0].section_number, "7.3")
        self.assertEqual(sections[0].source_ref.paragraph_index, 4)
        self.assertEqual(sections[0].text, "Фактическое содержание")

    def test_adjacent_duplicate_headings_keep_one_section(self):
        document = Document()
        document.add_paragraph("3.1.4. Оценочные средства")
        document.add_paragraph("")
        document.add_paragraph("3.1.4 Оценочные средства")
        document.add_paragraph("Пример задания: сравните алгоритмы.")

        sections, issues = extract_document_sections(document, "duplicate.docx")

        self.assertEqual(issues, [])
        self.assertEqual(len(sections), 1)
        self.assertEqual(sections[0].source_ref.paragraph_index, 1)
        self.assertEqual(sections[0].text, "Пример задания: сравните алгоритмы.")

    def test_unnumbered_heading_is_saved_without_inventing_a_number(self):
        document = Document()
        document.add_paragraph("Структура и содержание дисциплины", "Heading 2")
        document.add_paragraph("Описание темы")
        document.add_paragraph("Следующий раздел", "Heading 2")
        document.add_paragraph("Не включать")

        sections, issues = extract_document_sections(document, "unnumbered.docx")

        self.assertEqual(issues, [])
        self.assertEqual(len(sections), 1)
        self.assertEqual(sections[0].section_number, "")
        self.assertEqual(sections[0].title, "Структура и содержание дисциплины")
        self.assertIsNone(sections[0].parent_section)
        self.assertEqual(sections[0].text, "Описание темы")

    def test_rpp_text_stages_criteria_and_examples_are_saved(self):
        document = Document()
        document.add_paragraph("2.2. Структура и содержание практики")
        document.add_paragraph("Подготовительный этап: согласовать тему исследования.")
        document.add_paragraph("3.1.1. Виды и формы контроля и аттестации")
        document.add_paragraph("Зачет.")
        document.add_paragraph("3.1.3.2. Методика и критерии оценивания")
        document.add_paragraph("Для оценки «зачтено» необходимо выполнить все требования.")
        document.add_paragraph("3.1.3.3. Оценочные средства: задания")
        document.add_paragraph("Пример: подготовьте отчет и защитите результаты.")
        document.add_paragraph("3.1.3.4. Рекомендуемая форма отчета")
        document.add_paragraph("Не относится к оценочным средствам.")

        sections, issues = extract_document_sections(document, "practice.docx", doc_type="RPP")

        self.assertEqual(issues, [])
        self.assertEqual([section.section_number for section in sections], [
            "2.2", "3.1.3.2", "3.1.3.3",
        ])
        self.assertEqual([section.parent_section for section in sections], [
            "2", "3.1.3", "3.1.3",
        ])
        self.assertEqual([section.text for section in sections], [
            "Подготовительный этап: согласовать тему исследования.",
            "Для оценки «зачтено» необходимо выполнить все требования.",
            "Пример: подготовьте отчет и защитите результаты.",
        ])

    def test_styled_subheading_text_is_kept_even_without_its_own_body(self):
        document = Document()
        document.add_paragraph("2.2. Структура и содержание дисциплины")
        document.add_paragraph("Основы машинного обучения", "Heading 3")
        document.add_paragraph("2.3. Следующий раздел")

        sections, issues = extract_document_sections(document, "styled.docx")

        self.assertEqual(issues, [])
        self.assertEqual(sections[0].text, "Основы машинного обучения")

    def test_topic_numbering_does_not_cut_off_course_descriptions(self):
        document = Document()
        document.add_paragraph("2.2. Структура и содержание дисциплины")
        texts = (
            "1.1. Введение в операционные системы.",
            "Описание первой темы.",
            "Раздел 1. Классификация вычислительных систем.",
            "Раздел 3. Современные тенденции развития систем.",
            "2.1.1. Основные характеристики нечетких множеств.",
            "2.3. Средства синхронизации.",
            "2.3.1. Логические операции.",
            "Описание последней темы.",
        )
        for text in texts:
            document.add_paragraph(text)
        document.add_paragraph("Раздел 3. Обеспечение учебных занятий")
        document.add_paragraph("Не включать в содержание.")

        sections, issues = extract_document_sections(document, "numbered.docx")

        self.assertEqual(issues, [])
        self.assertEqual(sections[0].text, "\n\n".join(texts))

    def test_numbered_answers_and_heading_styles_do_not_cut_off_tasks(self):
        document = Document()
        document.add_paragraph("3.1.4. Оценочные средства")
        document.add_paragraph("Пример задания: организуйте локальную сеть.", "Heading 3")
        document.add_paragraph("Задание 4", "Heading 1")
        texts = (
            "Найдите оптимальное решение задачи.",
            "3.3 В;",
            "1.8 В;",
            "12 В.",
            "4.1. Объясните полученный результат.",
        )
        for text in texts:
            document.add_paragraph(text)
        document.add_paragraph(
            "3.1.5. Методические материалы для оценки обучающимися содержания "
            "и качества учебного процесса"
        )
        document.add_paragraph("Не включать в задания.")

        sections, issues = extract_document_sections(document, "tasks.docx")

        self.assertEqual(issues, [])
        self.assertEqual(sections[0].text, "\n\n".join((
            "Пример задания: организуйте локальную сеть.", "Задание 4", *texts,
        )))

    def test_nontext_content_is_reported_even_without_surrounding_text(self):
        for with_text in (False, True):
            with self.subTest(with_text=with_text):
                document = Document()
                document.add_paragraph("3.1.4. Оценочные средства")
                formula = OxmlElement("m:oMath")
                run = OxmlElement("m:r")
                text = OxmlElement("m:t")
                text.text = "x+1"
                run.append(text)
                formula.append(run)
                document.add_paragraph()._p.append(formula)
                if with_text:
                    document.add_paragraph("Решите уравнение.")

                sections, issues = extract_document_sections(document, "formula.docx")

                self.assertEqual(len(issues), 1)
                self.assertEqual(issues[0].category, "manual_review")
                self.assertEqual(issues[0].source_ref.paragraph_index, 1)
                self.assertIn("формулы", issues[0].message)
                if with_text:
                    self.assertEqual(sections[0].text, "Решите уравнение.")
                else:
                    self.assertEqual(sections, [])

    def test_missing_or_empty_sections_do_not_create_empty_records(self):
        document = Document()
        sections, issues = extract_document_sections(document, "empty.docx")
        self.assertEqual((sections, issues), ([], []))
        document.add_paragraph("2.2. Структура и содержание дисциплины")
        document.add_paragraph("2.3. Следующий раздел")
        sections, issues = extract_document_sections(document, "empty.docx")
        self.assertEqual((sections, issues), ([], []))

    def test_document_type_is_checked(self):
        with self.assertRaises(ValueError):
            extract_document_sections(Document(), "plan.docx", doc_type="CURRICULUM")

    def test_entrypoints_serialize_sections_with_existing_v2_contract(self):
        for filename, parser, model, numbers in (
            ("РПД_матанализ1.docx", parse_rpd, RPDOutput, ["2.2", "3.1.3", "3.1.4"]),
            ("РПП.docx", parse_rpp, RPPOutput, ["2.2", "3.1.3.2"]),
        ):
            with self.subTest(filename=filename):
                path = ROOT / "tests" / "originals" / filename
                output = parser(path, program_id="26/5003/1")
                document = Document(path)
                paragraphs = list(document.element.body.iter(qn("w:p")))

                self.assertTrue(output.document_sections)
                self.assertEqual(
                    [section.section_number for section in output.document_sections], numbers,
                )
                self.assertEqual(output.meta.schema_version, 2)
                self.assertEqual(model.model_validate_json(output.model_dump_json()), output)
                for section in output.document_sections:
                    self.assertTrue(section.text.strip())
                    self.assertEqual(section.source_ref.file, filename)
                    paragraph = paragraphs[section.source_ref.paragraph_index - 1]
                    self.assertEqual(
                        section.source_ref.section,
                        "".join(node.text or "" for node in paragraph.iter(qn("w:t"))).strip(),
                    )


if __name__ == "__main__":
    unittest.main()
