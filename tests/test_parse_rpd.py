import unittest
from collections import Counter
from pathlib import Path

from docx import Document
from docx.oxml import OxmlElement
from docx.oxml.ns import qn

from backend.contracts.document_v2.models import RPDOutput
from backend.worker.parser.assessment import extract_score_scale
from backend.worker.parser.content import extract_course_content
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
            "РПД_языки программирования.docx": (4, 13, 39, 38, 2, 6),
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

    def test_missing_hours_are_reported(self):
        output = self.outputs["РПД_языки программирования.docx"]

        self.assertTrue(any(
            issue.category == "empty_cell"
            and issue.source_ref is not None
            and issue.source_ref.table == 4
            and issue.source_ref.row == 31
            and issue.source_ref.col == 4
            for issue in output.parse_issues
        ))

        self.assertFalse(any(
            topic.source_ref.table == 4
            and topic.source_ref.row == 31
            for topic in output.course_content
        ))

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


if __name__ == "__main__":
    unittest.main()
