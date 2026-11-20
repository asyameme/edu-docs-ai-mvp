import tempfile
import unittest
from collections import Counter
from hashlib import sha256
from pathlib import Path

from docx import Document

from backend.contracts.document_v2.models import CurriculumOutput
from backend.worker.parser.parse_curriculum import parse_curriculum


ROOT = Path(__file__).resolve().parents[1]


def curriculum_document(semester=1):
    document = Document()
    document.add_paragraph("КОМПЕТЕНТНОСТНО-ОРИЕНТИРОВАННЫЙ УЧЕБНЫЙ ПЛАН")
    document.add_paragraph("Раздел 5. Формируемые компетенции")
    catalog = document.add_table(rows=2, cols=2)
    for cell, text in zip(catalog.rows[0].cells, (
        "Код компетенции", "Наименование и описание компетенции",
    )):
        cell.text = text
    catalog.cell(1, 0).text = "ОПК-1"
    catalog.cell(1, 1).text = "Способен применять математические методы"
    document.add_paragraph("Раздел 8. Организация обучения и итоговой аттестации")
    table = document.add_table(rows=1, cols=7)
    for cell, text in zip(table.rows[0].cells, (
        "Код Блока", "ЗЕТ", "Код компетенции", "Наименование дисциплины",
        "Лекции, часов", "Самостоятельная работа, часов",
        "Объем занятий в активных и интерактивных формах, часов",
    )):
        cell.text = text
    if semester is not None:
        cells = table.add_row().cells
        cells[0].merge(cells[-1]).text = f"Семестр {semester}"
    for cell, text in zip(table.add_row().cells, (
        "Блок.1.дисц", "6", "ОПК-1",
        "[001856] Математический анализ I\nMathematical Analysis I",
        "62", "154", "62",
    )):
        cell.text = text
    document.add_paragraph("Раздел 9. Дополнительная информация")
    return document, table, catalog


class TestCurriculumParser(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.original_path = ROOT / "tests/originals/UP.docx"
        cls.output = parse_curriculum(cls.original_path)

    def parse_document(self, document):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "plan.docx"
            document.save(path)
            return parse_curriculum(path)

    def test_original_competencies_and_semesters(self):
        self.assertEqual(len(self.output.competencies), 49)
        self.assertEqual(len(self.output.curriculum_items), 226)
        self.assertEqual(
            {item.semester for item in self.output.curriculum_items},
            set(range(1, 9)),
        )
        math, = [
            item for item in self.output.curriculum_items
            if item.discipline_name == "Математический анализ I"
        ]
        self.assertEqual((math.semester, math.credit_units, math.hours_total), (1, 6, 216))
        self.assertEqual((math.source_ref.table, math.source_ref.row, math.source_ref.col), (4, 7, 4))
        self.assertEqual({
            relation.dst_key for relation in self.output.relations
            if relation.src_key == math.discipline_name
        }, {"ОПК-1", "ПКА-1", "ПКП-УНК-03", "УК-1"})

    def test_original_practice_semesters_and_alternatives(self):
        rows = [
            item for item in self.output.curriculum_items
            if item.discipline_name.startswith("Учебная практика (проектно-технологическая)")
        ]
        self.assertEqual([
            (row.semester, row.credit_units, row.hours_total) for row in rows
        ], [(2, 3, 108), (3, 3, 108), (4, 6, 216), (5, 6, 216), (5, 6, 216)])
        self.assertIn("Профиль СТАНДАРТ", rows[-2].source_ref.section)
        self.assertIn("Профиль ТОП ИТ ПРО", rows[-1].source_ref.section)
        self.assertNotEqual(rows[-2].source_ref.row, rows[-1].source_ref.row)

    def test_original_zero_credit_rows_keep_hours_sources_and_relations(self):
        rows = [item for item in self.output.curriculum_items if item.credit_units == 0]
        source_rows = [*range(112, 117), *range(165, 170)]
        self.assertEqual(
            [(item.semester, item.credit_units, item.hours_total,
              item.source_ref.table, item.source_ref.row, item.source_ref.col) for item in rows],
            [(semester, 0, 164, 4, row, 4)
             for semester, numbers in ((3, range(112, 117)), (4, range(165, 170)))
             for row in numbers],
        )
        self.assertTrue(all(
            item.discipline_name.startswith("Физическая культура и спорт") for item in rows
        ))
        self.assertFalse(any(
            issue.category == "unexpected_format" and "credit_units = '0'" in issue.message
            for issue in self.output.parse_issues
        ))
        links = [
            relation for relation in self.output.relations
            if relation.source_ref.table == 4 and relation.source_ref.row in source_rows
        ]
        self.assertEqual(
            [(link.dst_key, link.source_ref.row, link.source_ref.col) for link in links],
            [("УК-7", row, 3) for row in source_rows],
        )

    def test_original_missing_codes_and_facultative_block_are_visible(self):
        self.assertTrue(any(
            issue.category == "empty_cell" and "нет кодов компетенций" in issue.message
            for issue in self.output.parse_issues
        ))
        self.assertTrue(any(
            item.block == "Факультативные занятия"
            for item in self.output.curriculum_items
        ))

    def test_metadata_round_trip_and_relation_endpoints(self):
        output = self.output
        self.assertEqual(output.meta.doc_type, "CURRICULUM")
        self.assertEqual(output.meta.schema_version, 2)
        self.assertEqual(output.meta.parser_config, "curriculum_docx_v4")
        self.assertEqual(output.meta.file_hash, sha256(self.original_path.read_bytes()).hexdigest())
        self.assertEqual(CurriculumOutput.model_validate_json(output.model_dump_json()), output)
        names = {item.discipline_name for item in output.curriculum_items}
        codes = {item.code for item in output.competencies}
        document = Document(self.original_path)
        for relation in output.relations:
            self.assertEqual((relation.src_entity, relation.dst_entity, relation.kind), (
                "curriculum_item", "competency", "формирует",
            ))
            self.assertIn(relation.src_key, names)
            self.assertIn(relation.dst_key, codes)
            row = document.tables[relation.source_ref.table - 1].rows[
                relation.source_ref.row - 1
            ]
            self.assertIn(relation.dst_key, row.cells[relation.source_ref.col - 1].text)

    def test_renumbered_sections_components_and_source_coordinates(self):
        document, table, _ = curriculum_document()
        output = self.parse_document(document)
        self.assertFalse(output.parse_issues)
        item, = output.curriculum_items
        self.assertEqual(item.discipline_name, "Математический анализ I")
        self.assertEqual(item.hours_total, 216)
        self.assertEqual((item.source_ref.table, item.source_ref.row), (2, 3))
        self.assertIn("Раздел 8.", item.source_ref.section)

    def test_changed_credits_and_hours_are_not_repaired(self):
        document, table, _ = curriculum_document()
        table.rows[-1].cells[1].text = "3"
        table.rows[-1].cells[5].text = "144"
        output = self.parse_document(document)
        item, = output.curriculum_items
        self.assertEqual((item.credit_units, item.hours_total), (3, 206))

    def test_zero_credits_keep_explicit_workload(self):
        for lecture_hours, self_study_hours, expected in ((62, 154, 216), (0, 0, 0)):
            with self.subTest(hours_total=expected):
                document, table, _ = curriculum_document()
                cells = table.rows[-1].cells
                cells[1].text = "0"
                cells[4].text = str(lecture_hours)
                cells[5].text = str(self_study_hours)
                cells[6].text = str(lecture_hours)
                output = self.parse_document(document)

                self.assertFalse(output.parse_issues)
                item, = output.curriculum_items
                self.assertEqual((item.credit_units, item.hours_total), (0, expected))
                self.assertEqual(
                    (item.source_ref.table, item.source_ref.row, item.source_ref.col), (2, 3, 4),
                )
                self.assertEqual([relation.dst_key for relation in output.relations], ["ОПК-1"])
                self.assertEqual(
                    CurriculumOutput.model_validate_json(output.model_dump_json()), output,
                )

    def test_invalid_credits_are_not_replaced_with_zero(self):
        for value in ("", "-1", "1,5", "не указано"):
            with self.subTest(credit_units=value):
                document, table, _ = curriculum_document()
                table.rows[-1].cells[1].text = value
                output = self.parse_document(document)

                self.assertFalse(output.curriculum_items)
                self.assertFalse(output.relations)
                self.assertTrue(any(
                    issue.category == ("empty_cell" if not value else "unexpected_format")
                    and issue.source_ref.table == 2
                    and issue.source_ref.row == 3
                    and issue.source_ref.col == 2
                    for issue in output.parse_issues
                ))

    def test_unknown_competency_does_not_create_dangling_relation(self):
        document, table, _ = curriculum_document()
        table.rows[-1].cells[2].text = "ОПК-1, КСК-67"
        output = self.parse_document(document)
        self.assertEqual(len(output.curriculum_items), 1)
        self.assertEqual([item.dst_key for item in output.relations], ["ОПК-1"])
        self.assertTrue(any(
            issue.category == "manual_review" and "КСК-67" in issue.message
            and issue.source_ref.col == 3
            for issue in output.parse_issues
        ))

    def test_missing_hours_are_not_zero_or_credits_times_36(self):
        document, table, _ = curriculum_document()
        table.rows[-1].cells[5].text = ""
        output = self.parse_document(document)
        self.assertFalse(output.curriculum_items)
        self.assertFalse(output.relations)
        self.assertTrue(any(
            issue.category == "empty_cell" and issue.source_ref.col == 6
            for issue in output.parse_issues
        ))

    def test_fractional_and_range_values_are_not_rounded(self):
        for column, value in [(1, "1,5"), (4, "6-8")]:
            with self.subTest(value=value):
                document, table, _ = curriculum_document()
                table.rows[-1].cells[column].text = value
                output = self.parse_document(document)
                self.assertFalse(output.curriculum_items)
                self.assertTrue(any(
                    issue.category == "unexpected_format"
                    and value in issue.message
                    and issue.source_ref.col == column + 1
                    for issue in output.parse_issues
                ))

    def test_overlapping_or_unknown_hour_column_is_not_summed(self):
        document, table, _ = curriculum_document()
        table.rows[0].cells[6].text = "Лекции, в том числе онлайн, часов"
        output = self.parse_document(document)
        self.assertFalse(output.curriculum_items)
        self.assertTrue(any(
            issue.category == "unexpected_format" and "колонки часов" in issue.message
            for issue in output.parse_issues
        ))

    def test_merged_hour_cells_do_not_double_count(self):
        document, table, _ = curriculum_document()
        table.rows[-1].cells[4].merge(table.rows[-1].cells[5]).text = "216"
        output = self.parse_document(document)
        self.assertFalse(output.curriculum_items)
        self.assertTrue(any(
            issue.category == "ambiguous" and "несколько видов нагрузки" in issue.message
            for issue in output.parse_issues
        ))

    def test_missing_semester_is_not_guessed(self):
        document, _, _ = curriculum_document(semester=None)
        output = self.parse_document(document)
        self.assertFalse(output.curriculum_items)
        self.assertTrue(any("не определен семестр" in issue.message for issue in output.parse_issues))

    def test_unknown_semester_does_not_reuse_previous_period(self):
        for text in ("Семестр 9", "Семестр 1/2"):
            with self.subTest(text=text):
                document, table, _ = curriculum_document()
                cells = table.add_row().cells
                cells[0].merge(cells[-1]).text = text
                for cell, value in zip(table.add_row().cells, (
                    "Блок.1.дисц", "3", "ОПК-1", "Другая дисциплина", "20", "88", "0",
                )):
                    cell.text = value
                output = self.parse_document(document)
                self.assertEqual(len(output.curriculum_items), 1)
                self.assertTrue(any(
                    "семестр" in issue.message or "семестра" in issue.message
                    for issue in output.parse_issues
                ))

    def test_explicit_total_and_semester_column(self):
        document, table, _ = curriculum_document(semester=None)
        # Расположение колонок отличается от Report.docx.
        headers = (
            "Наименование дисциплины", "Код Блока", "Код компетенции",
            "ЗЕТ", "Семестр", "Всего часов", "Лекции, часов",
        )
        values = ("Проверяемая дисциплина", "Блок.1.дисц", "ОПК-1", "3", "4", "206", "62")
        for cell, text in zip(table.rows[0].cells, headers):
            cell.text = text
        for cell, text in zip(table.rows[1].cells, values):
            cell.text = text
        output = self.parse_document(document)
        item, = output.curriculum_items
        self.assertEqual((item.semester, item.hours_total, item.credit_units), (4, 206, 3))
        self.assertEqual(item.source_ref.col, 1)

    def test_duplicate_catalog_rows_are_preserved(self):
        document, _, catalog = curriculum_document()
        cells = catalog.add_row().cells
        cells[0].text = "ОПК-1"
        cells[1].text = "Другая формулировка"
        output = self.parse_document(document)
        self.assertEqual(Counter(c.code for c in output.competencies), {"ОПК-1": 2})
        self.assertNotEqual(output.competencies[0].source_ref.row, output.competencies[1].source_ref.row)

    def test_missing_required_section_is_reported(self):
        document, _, _ = curriculum_document()
        for p in document.paragraphs:
            if "Формируемые компетенции" in p.text:
                p.text = "Раздел 5. Неизвестный раздел"
        output = self.parse_document(document)
        self.assertFalse(output.competencies)
        self.assertFalse(output.relations)
        self.assertEqual(len(output.curriculum_items), 1)
        self.assertTrue(any(issue.category == "not_found" for issue in output.parse_issues))

    def test_rpd_and_non_docx_are_rejected(self):
        with self.assertRaisesRegex(ValueError, "по титулу"):
            parse_curriculum(ROOT / "tests/originals/РПД_матанализ1.docx")
        with self.assertRaisesRegex(ValueError, "DOCX"):
            parse_curriculum(ROOT / "tests/originals/UP.xlsx")


if __name__ == "__main__":
    unittest.main()
