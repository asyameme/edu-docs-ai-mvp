import unittest
from collections import Counter
from pathlib import Path

from backend.worker.parser.models import ParserOutput
from backend.worker.parser.parse_rpd import parse_rpd


ROOT = Path(__file__).resolve().parents[1]


class TestRpdParser(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.outputs = {
            name: parse_rpd(ROOT / "tests" / "originals" / name)
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
                restored = ParserOutput.model_validate_json(
                    output.model_dump_json()
                )
                self.assertEqual(restored, output)

    def test_changed_workload_is_preserved(self):
        output = parse_rpd(
            ROOT / "tests/stage_1/case_1/РПД_матанализ1.docx"
        )
        discipline = output.disciplines[0]

        self.assertEqual(discipline.credit_units, 3)
        self.assertEqual(discipline.hours_total, 206)
        self.assertEqual(sum(discipline.hours_by_kind.values()), 206)

    def test_changed_competencies_are_preserved(self):
        output = parse_rpd(
            ROOT / "tests/stage_1/case_4/РПД_матанализ1.docx"
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