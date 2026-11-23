import unittest
from collections import Counter
from pathlib import Path
from tempfile import TemporaryDirectory

from docx import Document
from docx.oxml import OxmlElement
from docx.oxml.ns import qn

from backend.contracts.document_v2.models import Indicator, RPDOutput, SourceRef
from backend.worker.parser.assessment import extract_assessment_tools, extract_score_scale
from backend.worker.parser.content import extract_course_content
from backend.worker.parser.discipline import extract_workload
from backend.worker.parser.parse_rpd import parse_rpd


ROOT = Path(__file__).resolve().parents[1]
PROGRAM_ID = "26/5003/1"


class TestRpdParser(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.outputs = {
            name: parse_rpd(ROOT / "tests" / "originals" / name, program_id=PROGRAM_ID)
            for name in (
                "РПД_матанализ1.docx",
                "РПД_БД.docx",
                "РПД_языки программирования.docx",
            )
        }

    def test_original_documents(self):
        # Компетенции, индикаторы, результаты, темы, средства, шкалы.
        expected = {
            "РПД_матанализ1.docx": (4, 7, 9, 12, 2, 12),
            "РПД_БД.docx": (5, 12, 31, 8, 2, 0),
            "РПД_языки программирования.docx": (4, 13, 39, 39, 2, 6),
        }

        for name, counts in expected.items():
            with self.subTest(file=name):
                output = self.outputs[name]
                actual = (
                    len(output.competencies),
                    len(output.indicators),
                    len(output.learning_outcomes),
                    len(output.course_content),
                    len(output.assessment_tools),
                    len(output.score_scale),
                )
                self.assertEqual(actual, counts)

    def test_json_round_trip(self):
        for name, output in self.outputs.items():
            with self.subTest(file=name):
                restored = RPDOutput.model_validate_json(
                    output.model_dump_json()
                )
                self.assertEqual(restored, output)
                self.assertEqual(restored.meta.schema_version, 2)
                self.assertEqual(restored.meta.program_id, PROGRAM_ID)

    def test_entrypoint_uses_shared_modules(self):
        from backend.worker.parser import assessment, common, content, discipline, results
        from backend.worker.parser import parse_rpd as entrypoint

        for module, name in (
            (common, "normalize"),
            (discipline, "build_discipline"),
            (content, "extract_course_content"),
            (assessment, "extract_assessment_tools"),
            (results, "extract_results_into"),
        ):
            self.assertIs(getattr(entrypoint, name), getattr(module, name))

    def test_changed_workload_is_preserved(self):
        output = parse_rpd(
            ROOT / "tests/stage_1/case_1/РПД_матанализ1.docx",
            program_id=PROGRAM_ID,
        )
        discipline = output.disciplines[0]

        self.assertEqual(discipline.credit_units, 3)
        self.assertEqual(discipline.hours_total, 206)
        self.assertEqual(sum(discipline.hours_by_kind.values()), 206)
        self.assertEqual(output.title_credit_units, 6)
        self.assertIsNotNone(output.title_credit_units_source_ref.paragraph_index)

    def test_workload_with_shifted_merged_cells(self):
        document = Document()
        document.add_paragraph("2.1. Организация учебных занятий")
        table = document.add_table(rows=4, cols=6)
        header_layout = ((0, 0), (1, 2), (3, 4), (5, 5))
        data_layout = ((0, 1), (2, 2), (3, 3), (4, 5))
        rows = (
            (header_layout, (
                "Период обучения", "Контактная работа", "Активные формы", "Трудоемкость",
            )),
            (header_layout, (
                "Период обучения", "лекции", "Активные формы", "Трудоемкость",
            )),
            (data_layout, ("Семестр 3", "26", "26", "3")),
            (data_layout, ("ИТОГО", "26", "26", "3")),
        )
        for row_index, (layout, texts) in enumerate(rows):
            for (start, end), text in zip(layout, texts, strict=True):
                cell = table.cell(row_index, start)
                if start != end:
                    cell = cell.merge(table.cell(row_index, end))
                cell.text = text

        values, issues = extract_workload(document, "workload.docx")
        self.assertEqual(issues, [])
        expected_columns = (
            ("лекции", "hours", 26, 3),
            ("Активные формы", "reference", 26, 4),
            ("Трудоемкость", "credit_units", 3, 5),
        )
        self.assertEqual(
            [
                (
                    item["periods"], item["label"], item["kind"], item["value"],
                    item["source_ref"].row, item["source_ref"].col,
                )
                for item in values
            ],
            [
                ([period], label, kind, value, row, col)
                for row, period in ((3, "Семестр 3"), (4, "ИТОГО"))
                for label, kind, value, col in expected_columns
            ],
        )

    def test_language_from_title_is_kept_for_validation(self):
        output = parse_rpd(
            ROOT / "tests/stage_1/case_6/РПД_БД.docx",
            program_id=PROGRAM_ID,
        )
        self.assertEqual(output.education_languages, ["французский"])
        self.assertIsNotNone(output.education_languages_source_ref.paragraph_index)

    def test_content_with_consistently_omitted_trailing_cells(self):
        document = Document()
        document.add_paragraph("2.2. Структура и содержание учебных занятий")
        table = document.add_table(rows=2, cols=4)
        for row, values in zip(table.rows, (
            ("Тема", "Вид занятий", "Количество часов"),
            ("Кинематика", "лекции", "8"),
        )):
            for cell, value in zip(row.cells, values):
                cell.text = value
            row._tr.remove(row.cells[-1]._tc)
            trailing = OxmlElement("w:gridAfter")
            trailing.set(qn("w:val"), "1")
            row._tr.get_or_add_trPr().append(trailing)

        topics, issues = extract_course_content(document, "physics.docx")
        self.assertEqual(issues, [])
        self.assertEqual([(topic.topic, topic.hours) for topic in topics], [("Кинематика", 8)])

    def test_content_table_without_hours_keeps_topics_and_source_rows(self):
        document = Document()
        document.add_paragraph("2.2. Структура и содержание учебных занятий")
        table = document.add_table(rows=3, cols=3)
        for row, values in zip(table.rows, (
            ("№ п/п", "Наименование темы (раздела, части)", "Вид учебных занятий"),
            ("1", "Кинематика", "лекции"),
            ("1", "Кинематика", "практические занятия"),
        )):
            for cell, value in zip(row.cells, values):
                cell.text = value
        table.cell(1, 1).merge(table.cell(2, 1)).text = "Кинематика"

        topics, issues = extract_course_content(document, "physics.docx")
        self.assertEqual(
            [(topic.topic, topic.activity, topic.hours) for topic in topics],
            [("Кинематика", "лекции", None), ("Кинематика", "практические занятия", None)],
        )
        self.assertEqual(
            [(topic.source_ref.table, topic.source_ref.row, topic.source_ref.col) for topic in topics],
            [(1, 2, 2), (1, 3, 2)],
        )
        self.assertEqual(len(issues), 1)
        self.assertEqual(issues[0].category, "manual_review")
        self.assertEqual(issues[0].source_ref.table, 1)

    def test_headerless_ects_scale_preserves_source_values(self):
        document = Document()
        document.add_paragraph("3.1.3. Методика и критерии оценивания")
        table = document.add_table(rows=6, cols=3)
        grades = ("отлично", "хорошо", "хорошо", "удовлетворительно",
                  "удовлетворительно", "неудовлетворительно")
        for row, letter, grade in zip(table.rows, "ABCDEF", grades):
            row.cells[0].text = f"Критерий {letter}"
            row.cells[1].text = letter
            row.cells[2].text = grade

        items, issues = extract_score_scale(document, "scale.docx")
        self.assertEqual(issues, [])
        self.assertEqual([item.grade_spbu for item in items], list(grades))
        self.assertEqual([item.grade_ects for item in items], list("ABCDEF"))
        self.assertEqual([item.criterion for item in items], [f"Критерий {x}" for x in "ABCDEF"])
        self.assertEqual([item.source_ref.row for item in items], list(range(1, 7)))

    def test_changed_competencies_are_preserved(self):
        output = parse_rpd(
            ROOT / "tests/stage_1/case_4/РПД_матанализ1.docx",
            program_id=PROGRAM_ID,
        )
        codes = Counter(item.code for item in output.competencies)
        indicators = {item.code for item in output.indicators}

        self.assertEqual(codes["ОПК-1"], 2)
        self.assertNotIn("ПКА-1", codes)
        self.assertIn("УК-1.3", indicators)
        self.assertNotIn("УК-1.4", indicators)

    def test_indicators_are_preserved_when_only_competency_code_is_given(self):
        name = "РПД_матанализ1.docx"
        document = Document(ROOT / "tests" / "originals" / name)
        document.tables[0].cell(1, 0).text = "УК-1"

        with TemporaryDirectory() as directory:
            path = Path(directory) / name
            document.save(path)
            output = parse_rpd(path, program_id=PROGRAM_ID)

        expected = [
            item for item in self.outputs[name].indicators
            if item.competency_code == "УК-1"
        ]
        self.assertTrue(expected)
        self.assertEqual(
            [item for item in output.indicators if item.competency_code == "УК-1"],
            expected,
        )
        self.assertNotIn("УК-1", {item.code for item in output.competencies})
        # Код без локальной формулировки не объявляет сущность, но строка
        # таблицы ссылается на компетенцию: связи «имеет» и «включает»
        # сохраняются, иначе результаты обучения теряют индикаторную цепочку.
        has_relation = [
            relation for relation in output.relations
            if relation.src_entity == "competency" and relation.src_key == "УК-1"
        ]
        self.assertTrue(any(relation.kind == "имеет" for relation in has_relation))
        self.assertTrue(any(
            relation.kind == "включает" and relation.dst_entity == "learning_outcome"
            for relation in has_relation
        ))
        self.assertTrue(any(
            issue.category == "manual_review"
            and "УК-1" in issue.message
            and issue.source_ref is not None
            and (issue.source_ref.table, issue.source_ref.row, issue.source_ref.col) == (1, 2, 1)
            for issue in output.parse_issues
        ))

    def test_content_keeps_topic_when_hours_cell_is_empty(self):
        output = self.outputs["РПД_языки программирования.docx"]

        self.assertTrue(any(
            issue.category == "empty_cell"
            and issue.source_ref is not None
            and issue.source_ref.table == 4
            and issue.source_ref.row == 31
            and issue.source_ref.col == 4
            for issue in output.parse_issues
        ))

        topic, = [
            item for item in output.course_content
            if item.source_ref.table == 4 and item.source_ref.row == 31
        ]
        self.assertEqual(topic.topic, "Основные оптимизации, применяемые компилятором")
        self.assertEqual(topic.activity, "по методическим материалам")
        self.assertIsNone(topic.hours)
        self.assertEqual(topic.source_ref.col, 2)

    def test_content_distinguishes_dashes_from_zero_and_invalid_hours(self):
        document = Document()
        document.add_paragraph("2.2. Структура и содержание учебных занятий")
        rows = (
            ("Тема", "Вид занятий", "Количество часов"),
            ("Кинематика", "лекции", "-"),
            ("Динамика", "лекции", "\u2014"),
            ("Статика", "лекции", "0"),
            ("Термодинамика", "лекции", "уточнить"),
        )
        table = document.add_table(rows=len(rows), cols=3)
        for row, values in zip(table.rows, rows, strict=True):
            for cell, value in zip(row.cells, values, strict=True):
                cell.text = value

        topics, issues = extract_course_content(document, "physics.docx")
        self.assertEqual(
            [(topic.topic, topic.hours, topic.source_ref.row) for topic in topics],
            [("Кинематика", None, 2), ("Динамика", None, 3), ("Статика", 0, 4)],
        )
        self.assertEqual(
            [(issue.category, issue.source_ref.row, issue.source_ref.col) for issue in issues],
            [("empty_cell", 2, 3), ("empty_cell", 3, 3), ("unexpected_format", 5, 3)],
        )

    def test_relations_have_existing_entities(self):
        for name, output in self.outputs.items():
            keys = {
                "discipline": {x.name for x in output.disciplines},
                "competency": {x.code for x in output.competencies},
                "indicator": {x.code for x in output.indicators},
                "learning_outcome": {
                    x.local_id for x in output.learning_outcomes
                },
                "assessment_tool": {
                    x.name for x in output.assessment_tools
                },
            }

            for relation in output.relations:
                with self.subTest(file=name, relation=relation):
                    self.assertIn(relation.src_entity, keys)
                    self.assertIn(relation.dst_entity, keys)
                    self.assertIn(
                        relation.src_key, keys[relation.src_entity]
                    )
                    self.assertIn(
                        relation.dst_key, keys[relation.dst_entity]
                    )


class TestAssessmentNumberingRows(unittest.TestCase):
    def extract(self, rows, doc_type):
        document = Document()
        document.add_table(rows=1, cols=1).cell(0, 0).text = "Титульная таблица"
        document.add_paragraph("3.1.4. Оценочные средства")
        table = document.add_table(rows=len(rows) + 1, cols=3)
        for cell, text in zip(table.rows[0].cells, (
            "№",
            "Код индикатора и индикатор достижения",
            "Контрольно-измерительные материалы",
        )):
            cell.text = text
        for row, values in zip(table.rows[1:], rows):
            for cell, text in zip(row.cells, values):
                cell.text = text
        indicator = Indicator(
            code="УК-1.1", text="Анализирует задачу", competency_code="УК-1",
            source_ref=SourceRef(file="assessment.docx"),
        )
        return extract_assessment_tools(
            document, "assessment.docx", [indicator], doc_type=doc_type,
        )

    def test_numbering_header_does_not_create_tools_or_issues(self):
        for doc_type in ("RPD", "RPP"):
            for numbering in (("", "1", "2"), ("1", "2", "3"), ("", "2", "3")):
                with self.subTest(doc_type=doc_type, numbering=numbering):
                    tools, relations, issues = self.extract((
                        numbering,
                        ("1", "УК-1.1 Анализирует задачу", "Отчет"),
                    ), doc_type)

                    self.assertEqual(issues, [])
                    self.assertEqual([tool.name for tool in tools], ["Отчет"])
                    self.assertEqual(
                        [(t.source_ref.table, t.source_ref.row, t.source_ref.col) for t in tools],
                        [(2, 3, 3)],
                    )
                    self.assertEqual(
                        [(r.src_key, r.dst_key, r.source_ref.table, r.source_ref.row)
                         for r in relations],
                        [("УК-1.1", "Отчет", 2, 3)],
                    )

    def test_numeric_tool_name_with_real_indicator_is_kept(self):
        for doc_type in ("RPD", "RPP"):
            with self.subTest(doc_type=doc_type):
                tools, relations, issues = self.extract((
                    ("1", "УК-1.1 Анализирует задачу", "2"),
                ), doc_type)

                self.assertEqual(issues, [])
                self.assertEqual([tool.name for tool in tools], ["2"])
                self.assertEqual(
                    [(r.src_key, r.dst_key) for r in relations], [("УК-1.1", "2")],
                )
                self.assertEqual(tools[0].source_ref.row, 2)


if __name__ == "__main__":
    unittest.main()
