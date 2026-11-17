import json
import unittest
from collections import Counter
from pathlib import Path

from backend.worker.parser.adapters_v2 import merge_graphs
from backend.worker.parser.parse_characteristic import parse_characteristic
from backend.worker.parser.parse_ckmv import parse_ckmv


ROOT = Path(__file__).resolve().parents[1]
ORIGINALS = ROOT / "tests/originals"
PROGRAM_ID = "26/5003/1"


class TestProgramDocuments(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.ckmv = parse_ckmv(ORIGINALS / "ЦКМВ.docx", program_id=PROGRAM_ID)
        cls.characteristic = parse_characteristic(
            ORIGINALS / "Общая характеристика.docx", program_id=PROGRAM_ID,
        )

    def test_characteristic_metadata_and_competencies(self):
        example = ROOT / "backend/worker/parser/json_examples_v2/characteristic_v2.json"
        expected = json.loads(example.read_text(encoding="utf-8"))
        program = self.characteristic.program.model_dump(exclude={"source_ref"})
        expected["program"].pop("source_ref")
        self.assertCountEqual(
            program.pop("professional_standards"), expected["program"].pop("professional_standards"),
        )
        self.assertEqual(program, expected["program"])
        self.assertEqual(
            [(item.code, item.formulation) for item in self.characteristic.competencies],
            [(item["code"], " ".join(item["formulation"].split())) for item in expected["competencies"]],
        )
        self.assertFalse(self.characteristic.parse_issues)

    def test_ckmv_preserves_roles_sections_and_repeated_competency_codes(self):
        ckmv = self.ckmv
        self.assertEqual(
            [role.name for role in ckmv.professional_roles],
            ["MLOps / Data Scientist", "Руководитель группы разработки"],
        )
        counts = Counter(item.code for item in ckmv.competencies)
        self.assertEqual(len(counts), 29)
        self.assertEqual(counts["ПКП-9-ППК-ДА2"], 2)
        self.assertEqual(counts["ПКП-10-ППК-ДА3"], 2)
        self.assertEqual(len(ckmv.indicators), 85)
        self.assertEqual(len(ckmv.learning_outcomes), 409)
        self.assertEqual(len(ckmv.indicator_outcome_links), 409)
        self.assertFalse(ckmv.parse_issues)
        roles = {role.local_id: role.name for role in ckmv.professional_roles}
        links = {(roles[item.role_id], item.competency_code) for item in ckmv.role_competency_links}
        self.assertIn(("MLOps / Data Scientist", "ПКП-МО-1"), links)
        self.assertIn(("Руководитель группы разработки", "ПКП-23-РГ-4"), links)
        self.assertTrue(all(not code.startswith("УНК-") for _, code in links))

    def test_two_outcomes_in_one_cell_keep_separate_sources(self):
        outcomes = [
            item for item in self.ckmv.learning_outcomes
            if item.source_ref.row == 59 and item.source_ref.col == 4
        ]
        self.assertEqual(
            [item.code for item in outcomes],
            ["ПКП-2-ППК-Р2.3. У-1", "ПКП-2-ППК-Р2.3. У-2"],
        )
        self.assertLess(outcomes[0].source_ref.paragraph_index, outcomes[1].source_ref.paragraph_index)
        ids = {item.local_id for item in outcomes}
        self.assertEqual(
            [link.indicator_code for link in self.ckmv.indicator_outcome_links if link.learning_outcome_id in ids],
            ["ПКП-2-ППК-Р2.3", "ПКП-2-ППК-Р2.3"],
        )

    def test_deleted_block_and_changed_outcome_codes_remain_visible(self):
        output = parse_ckmv(ROOT / "tests/stage_1/case_5/ЦКМВ.docx")
        self.assertTrue(any(
            issue.category == "empty_cell" and issue.source_ref.row == 11
            and "11-162" in issue.message for issue in output.parse_issues
        ))
        codes = {item.code for item in output.learning_outcomes}
        self.assertNotIn("УНК-01.02. З-3", codes)
        self.assertIn("УНК-03.02. З-4", codes)

    def test_changed_parent_code_remains_visible(self):
        output = parse_ckmv(ROOT / "tests/stage_1/case_7/ЦКМВ.docx")
        codes = {item.code for item in output.competencies}
        self.assertIn("ПКП-23-РГ-3", codes)
        self.assertNotIn("ПКП-23-РГ-4", codes)
        indicator = next(item for item in output.indicators if item.code == "ПКП-23-РГ-4.1")
        self.assertEqual(indicator.competency_code, "ПКП-23-РГ-3")
        self.assertTrue(any(
            issue.category == "unexpected_format" and issue.source_ref.row == indicator.source_ref.row
            for issue in output.parse_issues
        ))
        self.assertIn("ПКП-23-РГ-4.1. З-3", {item.code for item in output.learning_outcomes})

    def test_both_outputs_feed_the_v2_graph(self):
        graph = merge_graphs([
            self.ckmv.model_dump(mode="json"),
            self.characteristic.model_dump(mode="json"),
        ], program_id=PROGRAM_ID)
        self.assertTrue(any(node.entity_type == "program" for node in graph.nodes))
        self.assertEqual(sum(node.entity_type == "professional_role" for node in graph.nodes), 2)
        self.assertEqual(sum(node.entity_type == "learning_outcome" for node in graph.nodes), 409)


if __name__ == "__main__":
    unittest.main()
