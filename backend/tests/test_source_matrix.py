"""Неопределённость исходной связи не превращается в ноль производной матрицы."""

from io import BytesIO

from backend.contracts.document_v2.graph import EducationalGraph
from openpyxl import load_workbook
from test_generation_matrix import _node

from worker.generation import build_matrix_xlsx, read_matrix_xlsx
from worker.generation.matrix_xlsx import derive_matrix


def test_unresolved_pair_is_unknown_instead_of_zero():
    graph = EducationalGraph()
    graph.add_node(_node("discipline", "Дисциплина", {"credit_units": 3}))
    graph.add_node(_node("competency", "УК-1"))

    matrix = derive_matrix(graph, unresolved_pairs=[("Дисциплина", None, "УК-1")])

    assert matrix.rows[0].values == (None,)


def test_unknown_reference_code_and_legend_survive_xlsx_roundtrip():
    graph = EducationalGraph()
    graph.add_node(_node("discipline", "Дисциплина", {"credit_units": 3}))
    graph.add_node(_node("competency", "УК-1"))
    content = build_matrix_xlsx(
        graph,
        unresolved_pairs=[("Дисциплина", None, "УК-X")],
    )

    codes, values = read_matrix_xlsx(content)

    assert values[0][codes.index("УК-X")] is None
    assert values[0][codes.index("УК-1")] == 0
    workbook = load_workbook(BytesIO(content))
    assert "Примечания" in workbook.sheetnames
    assert "не определена" in workbook["Примечания"]["A1"].value
