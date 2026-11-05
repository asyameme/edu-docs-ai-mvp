import importlib
import json
import unittest
from pathlib import Path

from docx import Document
from docx.oxml.ns import qn
from pydantic import ValidationError

from backend.contracts import parser_output as contract
from backend.contracts.parser_output import (
    Competency, ParseIssue, ParserMeta, ParserOutput, SourceRef,
)
from backend.worker.parser.common import finalize_output, read_section
from backend.worker.parser.parse_curriculum import parse_curriculum
from backend.worker.parser.parse_rpd import parse_rpd
from backend.worker.parser.parse_rpp import parse_rpp


ROOT = Path(__file__).resolve().parents[1]


def source_document():
    document = Document()
    document.add_paragraph("Титул")                    # 1
    document.add_paragraph("1. Данные")                # 2
    document.add_paragraph("Первый источник")          # 3
    table = document.add_table(rows=1, cols=2)
    table.cell(0, 0).text = "Левая ячейка"              # 4
    table.cell(0, 1).text = "Правая ячейка"             # 5
    table.cell(0, 1).add_paragraph("Продолжение")        # 6
    document.add_paragraph("Второй источник")          # 7
    document.add_paragraph("2. Следующий раздел")       # 8
    document.sections[0].header.paragraphs[0].text = "Колонтитул"
    document.sections[0].footer.paragraphs[0].text = "Номер страницы"
    return document, table


def output_with_ref(ref):
    return ParserOutput(
        meta=ParserMeta(
            doc_type="RPD", file_name="example.docx", file_hash="a" * 64,
            parser_config="source_ref_test",
        ),
        competencies=[Competency(code="УК-1", formulation="Описание", source_ref=ref)],
        parse_issues=[ParseIssue(
            category="manual_review", message="Проблема файла",
            source_ref=SourceRef(file="example.docx"),
        )],
    )


def without_annotations(value):
    """Сравнить ограничения схем, сохранив имена полей и определения."""
    if isinstance(value, list):
        return [without_annotations(item) for item in value]
    if not isinstance(value, dict):
        return value
    result = {}
    for key, item in value.items():
        if key in {"title", "description"}:
            continue
        if key in {"properties", "$defs"}:
            result[key] = {name: without_annotations(schema) for name, schema in item.items()}
        else:
            result[key] = without_annotations(item)
    return result


class TestParserContract(unittest.TestCase):
    def test_modules_import_the_same_shared_classes(self):
        for name in (
            "common", "results", "discipline", "content", "assessment",
            "parse_rpd", "parse_rpp", "parse_curriculum",
        ):
            module = importlib.import_module("backend.worker.parser." + name)
            for cls_name in ("ParserOutput", "ParseIssue", "SourceRef", "Competency"):
                if hasattr(module, cls_name):
                    self.assertIs(getattr(module, cls_name), getattr(contract, cls_name))

    def test_schema_matches_shared_models(self):
        schema_path = ROOT / "backend/contracts/parser_contract.schema.json"
        schema = json.loads(schema_path.read_text())
        self.assertEqual(
            without_annotations(schema),
            without_annotations(ParserOutput.model_json_schema()),
        )

    def test_paragraph_index_is_optional_and_starts_at_one(self):
        self.assertIsNone(SourceRef(file="example.docx").paragraph_index)
        self.assertEqual(SourceRef(file="example.docx", paragraph_index=1).paragraph_index, 1)
        with self.assertRaises(ValidationError):
            SourceRef(file="example.docx", paragraph_index=0)

    def test_read_section_counts_cell_paragraphs_and_excludes_headers(self):
        document, _ = source_document()
        blocks, issues = read_section(
            document, "example.docx", lambda p: p.text == "1. Данные", "Данные",
        )
        self.assertFalse(issues)
        refs = {block.text: ref for block, ref in blocks if hasattr(block, "text")}
        self.assertEqual(refs["Первый источник"].paragraph_index, 3)
        self.assertEqual(refs["Второй источник"].paragraph_index, 7)

    def test_table_anchor_preserves_existing_coordinates(self):
        document, _ = source_document()
        output = finalize_output(output_with_ref(SourceRef(
            file="example.docx", table=1, row=1, col=2,
        )), document)
        ref = output.competencies[0].source_ref
        self.assertEqual((ref.table, ref.row, ref.col, ref.paragraph_index), (1, 1, 2, 5))
        self.assertIsNone(output.parse_issues[0].source_ref.paragraph_index)
        self.assertEqual(ParserOutput.model_validate_json(output.model_dump_json()), output)

    def test_empty_cell_has_its_own_paragraph(self):
        document, table = source_document()
        table.cell(0, 1).text = ""
        output = finalize_output(output_with_ref(SourceRef(
            file="example.docx", table=1, row=1, col=2,
        )), document)
        self.assertEqual(output.competencies[0].source_ref.paragraph_index, 5)

    def test_merged_cells_do_not_duplicate_global_paragraph_numbers(self):
        document, table = source_document()
        table.cell(0, 0).merge(table.cell(0, 1))
        blocks, _ = read_section(
            document, "example.docx", lambda p: p.text == "1. Данные", "Данные",
        )
        ref = next(ref for block, ref in blocks if getattr(block, "text", None) == "Второй источник")
        self.assertEqual(ref.paragraph_index, 7)

    def test_nested_tables_are_part_of_global_paragraph_order(self):
        document, table = source_document()
        nested = table.cell(0, 0).add_table(rows=1, cols=1)
        nested.cell(0, 0).text = "Вложенная таблица"
        # В ячейку добавились абзац вложенной таблицы и пустой абзац после нее.
        output = finalize_output(output_with_ref(SourceRef(
            file="example.docx", table=1, row=1, col=2,
        )), document)
        self.assertEqual(output.competencies[0].source_ref.paragraph_index, 7)
        blocks, _ = read_section(
            document, "example.docx", lambda p: p.text == "1. Данные", "Данные",
        )
        ref = next(ref for block, ref in blocks if getattr(block, "text", None) == "Второй источник")
        self.assertEqual(ref.paragraph_index, 9)

    def test_legacy_examples_are_accepted_by_new_contract(self):
        for path in (ROOT / "backend/worker/parser/json_examples").glob("*.json"):
            with self.subTest(path=path.name):
                output = ParserOutput.model_validate_json(path.read_text())
                self.assertEqual(ParserOutput.model_validate_json(output.model_dump_json()), output)

    def test_all_three_parsers_return_resolvable_paragraph_indices(self):
        cases = (
            ("РПД_матанализ1.docx", parse_rpd),
            ("РПП.docx", parse_rpp),
            ("UP.docx", parse_curriculum),
        )
        for name, parser in cases:
            with self.subTest(file=name):
                path = ROOT / "tests/originals" / name
                output = parser(path)
                self.assertIs(type(output), ParserOutput)
                document = Document(path)
                paragraphs = list(document.element.body.iter(qn("w:p")))
                for field in type(output).model_fields:
                    values = getattr(output, field)
                    if not isinstance(values, list):
                        continue
                    for record in values:
                        ref = record.source_ref
                        if ref is None:
                            continue
                        if ref.table is not None:
                            self.assertIsNotNone(ref.paragraph_index)
                            table = document.tables[ref.table - 1]
                            element = table._tbl
                            if ref.row is not None:
                                row = table.rows[ref.row - 1]
                                element = row._tr
                                if ref.col is not None:
                                    element = row.cells[ref.col - 1]._tc
                            self.assertIn(
                                paragraphs[ref.paragraph_index - 1],
                                list(element.iter(qn("w:p"))),
                            )
                        elif ref.paragraph_index is not None:
                            self.assertLessEqual(ref.paragraph_index, len(paragraphs))
                if name == "РПП.docx":
                    prose_competencies = [
                        c for c in output.competencies if c.source_ref.table is None
                    ]
                    self.assertTrue(prose_competencies)
                    for competency in prose_competencies:
                        ref = competency.source_ref
                        self.assertIsNotNone(ref.paragraph_index)
                        text = "".join(
                            node.text or "" for node in paragraphs[ref.paragraph_index - 1].iter(qn("w:t"))
                        )
                        self.assertIn(competency.code, text)


if __name__ == "__main__":
    unittest.main()
