
import json
import unittest
from pathlib import Path

from backend.contracts.document_v2.models import parse_document
from backend.worker.parser.adapters_v2.normalize import (
    merge_graphs,
    to_graph,
)

ROOT = Path(__file__).resolve().parents[2]
EXAMPLES = ROOT / "backend/worker/parser/json_examples_v2"

NAMES = (
    "characteristic_v2.json",
    "ckmv_v2.json",
    "curriculum_v2.json",
    "matrix_v2.json",
    "rpd_v2.json",
    "rpp_v2.json",
)

PID = "26/5003/1"


def load_document(name):
    path = EXAMPLES / name
    return parse_document(
        json.loads(path.read_text(encoding="utf-8"))
    )


class GraphV2Tests(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.documents = [
            load_document(name) for name in NAMES
        ]
        cls.graph = merge_graphs(cls.documents)
        cls.nodes = {
            node.id: node for node in cls.graph.nodes
        }

    def test_all_six_examples_parse_and_merge(self):
        self.assertEqual(len(self.documents), 6)
        self.assertEqual(len(self.nodes), len(self.graph.nodes))
        self.assertEqual(len(self.graph.nodes), 795)
        self.assertEqual(len(self.graph.edges), 1887)

        self.assertFalse(
            any(
                node.id.startswith("unassigned|")
                for node in self.graph.nodes
            )
        )

        for edge in self.graph.edges:
            self.assertIn(edge.source_id, self.nodes)
            self.assertIn(edge.target_id, self.nodes)

    def test_declared_competency_without_formulation(self):
        doc = load_document("characteristic_v2.json")
        competency = doc.competencies[0]
        original_ref = competency.source_ref

        doc.competencies[0] = competency.model_copy(
            update={"formulation": None}
        )

        graph = to_graph(doc)
        node_id = f"{PID}|competency|{competency.code}"
        nodes = {node.id: node for node in graph.nodes}

        self.assertIn(node_id, nodes)
        self.assertIn(
            original_ref,
            nodes[node_id].source_refs
        )
        self.assertNotIn(
            "formulation",
            nodes[node_id].attributes
        )

    def test_curriculum_links(self):
        doc = load_document("curriculum_v2.json")
        graph = to_graph(doc)

        items = {
            node.id
            for node in graph.nodes
            if node.entity_type == "curriculum_item"
        }

        self.assertTrue(items)

        self.assertTrue(
            all("::semester=" in item for item in items)
        )

        relations = [
            edge for edge in graph.edges
            if edge.source_id in items
            or edge.target_id in items
        ]

        self.assertTrue(relations)

        self.assertFalse(
            any(
                node.entity_type == "curriculum_item"
                and "::semester=" not in node.id
                for node in graph.nodes
            )
        )

    def test_course_content(self):
        doc = load_document("rpd_v2.json")
        graph = to_graph(doc)

        topics = {
            node.id: node
            for node in graph.nodes
            if node.entity_type == "course_content"
        }

        self.assertEqual(len(topics), len(doc.course_content))
        self.assertEqual(len(topics), 12)

        for index, topic in enumerate(doc.course_content):
            node_id = (
                f"{PID}|course_content|"
                f"{doc.meta.file_name}::content_row={index}"
            )

            self.assertIn(node_id, topics)
            self.assertEqual(
                topics[node_id].attributes["hours"],
                topic.hours
            )
            self.assertEqual(
                topics[node_id].attributes["topic"],
                topic.topic
            )

            self.assertTrue(
                any(
                    edge.target_id == node_id
                    and edge.relation_type == "содержит_тему"
                    for edge in graph.edges
                )
            )

    def test_program_id_mismatch(self):
        doc = load_document("rpd_v2.json")

        with self.assertRaisesRegex(
            ValueError, "Program ID mismatch"
        ):
            to_graph(doc, program_id="different-program")

    def test_missing_competency_definition(self):
        node_id = f"{PID}|competency|ПКП-1-ППК-Р1"

        self.assertIn(node_id, self.nodes)
        self.assertNotIn(
            "formulation",
            self.nodes[node_id].attributes
        )

        self.assertTrue(
            any(
                edge.target_id == node_id
                for edge in self.graph.edges
            )
        )


if __name__ == "__main__":
    unittest.main()
