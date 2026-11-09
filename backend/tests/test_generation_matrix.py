"""Тесты производной матрицы компетенций: проекция графа и XLSX-раскладка."""

import json
from io import BytesIO
from pathlib import Path

import pytest
from backend.contracts.document_v2.graph import EducationalGraph, GraphEdge, GraphNode
from backend.contracts.document_v2.models import parse_document
from backend.worker.generation import build_matrix_xlsx, derive_matrix, read_matrix_xlsx
from backend.worker.generation.matrix_xlsx import SHEET_NAME
from backend.worker.parser.adapters_v2.normalize import merge_graphs
from openpyxl import load_workbook

PID = "26/5003/1"
ROOT = Path(__file__).resolve().parents[2]
EXAMPLES = ROOT / "backend/worker/parser/json_examples_v2"


def _ref(row: int | None = None):
    from backend.contracts.document_v2.models import SourceRefV2

    return SourceRefV2(file="test.docx", row=row)


def _node(kind: str, key: str, attributes=None, row: int | None = None):
    return GraphNode(
        id=f"{PID}|{kind}|{key}",
        entity_type=kind,
        attributes=attributes or {},
        source_refs=[_ref(row)],
    )


def _edge(src_kind, src_key, dst_kind, dst_key, semester=None):
    return GraphEdge(
        source_id=f"{PID}|{src_kind}|{src_key}",
        target_id=f"{PID}|{dst_kind}|{dst_key}",
        relation_type="формирует",
        source_ref=_ref(),
        document_type="TEST",
        semester=semester,
    )


def _synthetic_graph() -> EducationalGraph:
    graph = EducationalGraph()
    for code in ("УК-1", "УК-2", "УК-10", "ОПК-1"):
        graph.add_node(_node("competency", code))
    graph.add_node(_node("discipline", "Теория вероятностей"))
    graph.add_node(
        _node("curriculum_item", "Дискретная математика::semester=1",
              {"credit_units": 4, "block": "Блок.1.дисц"}, row=7),
    )
    graph.add_node(
        _node("curriculum_item", "Математический анализ I::semester=1",
              {"credit_units": 6, "block": "Блок.1.дисц"}, row=5),
    )
    graph.add_node(
        _node("curriculum_item", "Математический анализ I::semester=2", {"credit_units": 6}),
    )
    # план: Матанализ I в 1-м семестре формирует УК-1
    graph.add_edge(_edge("curriculum_item", "Математический анализ I::semester=1",
                         "competency", "УК-1"))
    # РПД: дисциплина формирует ОПК-1 без указания семестра — действует на оба
    graph.add_edge(_edge("discipline", "Математический анализ I", "competency", "ОПК-1"))
    # семестр 2: только вторая строка Матанализа I
    graph.add_edge(_edge("discipline", "Математический анализ I", "competency", "УК-2", semester=2))
    return graph


def test_derive_orders_and_values():
    matrix = derive_matrix(_synthetic_graph())

    # натуральный порядок кодов: УК-1 < УК-2 < УК-10 < ОПК-1
    assert matrix.competency_codes == ("УК-1", "УК-2", "УК-10", "ОПК-1")
    # строки по семестру, внутри — по строке исходного плана (row 5 раньше row 7)
    assert [row.discipline for row in matrix.rows] == [
        "Математический анализ I",
        "Дискретная математика",
        "Математический анализ I",
    ]
    analysis_first, discrete, analysis_second = matrix.rows

    assert analysis_first.semester == 1
    assert analysis_first.part == "Блок.1.дисц"
    assert analysis_first.credits == 6
    assert analysis_first.values == (1, 0, 0, 1)  # УК-1 из плана, ОПК-1 из РПД

    assert discrete.values == (0, 0, 0, 0)  # связей нет — нули, строка не теряется

    assert analysis_second.semester == 2
    assert analysis_second.values == (0, 1, 0, 1)  # УК-2 семестра 2 + ОПК-1 без семестра


def test_derive_falls_back_to_disciplines():
    graph = EducationalGraph()
    graph.add_node(_node("competency", "УК-1"))
    graph.add_node(_node("competency", "УК-2"))
    graph.add_node(_node("discipline", "Практика", {"credit_units": 3}))

    matrix = derive_matrix(graph)

    assert len(matrix.rows) == 1
    row = matrix.rows[0]
    assert row.discipline == "Практика"
    assert row.semester is None
    assert row.credits == 3
    assert row.values == (0, 0)  # связей нет

    # строка без семестра — дисциплина в целом: ребро любого семестра даёт 1
    graph.add_edge(_edge("discipline", "Практика", "competency", "УК-2", semester=3))
    assert derive_matrix(graph).rows[0].values == (0, 1)


@pytest.mark.parametrize(
    "graph",
    [
        EducationalGraph(),
        EducationalGraph(nodes=[_node("competency", "УК-1")]),
        EducationalGraph(nodes=[_node("discipline", "Практика")]),
    ],
)
def test_derive_raises_without_axes(graph):
    with pytest.raises(ValueError):
        derive_matrix(graph)


def test_build_xlsx_layout():
    content = build_matrix_xlsx(_synthetic_graph())
    sheet = load_workbook(BytesIO(content))[SHEET_NAME]

    codes = [cell.value for cell in sheet[2][6:]]
    assert codes == ["УК-1", "УК-2", "УК-10", "ОПК-1"]
    assert sheet.cell(row=1, column=1).value == "Тип компетенции →"
    types = [sheet.cell(row=1, column=column).value for column in range(7, 11)]
    assert types == ["УК", "УК", "УК", "ОПК"]
    assert [cell.value for cell in sheet[2][:6]] == [
        "№ строки", "Семестр", "Профиль", "Часть", "ЗЕТ", "Дисциплина",
    ]

    header_row = 3
    assert sheet.cell(row=header_row, column=1).value == 1
    assert sheet.cell(row=header_row, column=2).value == 1
    assert sheet.cell(row=header_row, column=3).value is None  # «Профиль»: в графе нет
    assert sheet.cell(row=header_row, column=4).value == "Блок.1.дисц"
    assert sheet.cell(row=header_row, column=5).value == 6
    assert sheet.cell(row=header_row, column=6).value == "Математический анализ I"
    assert [sheet.cell(row=header_row, column=c).value for c in range(7, 11)] == [1, 0, 0, 1]

    codes_read, values_read = read_matrix_xlsx(content)
    assert codes_read == ("УК-1", "УК-2", "УК-10", "ОПК-1")
    assert values_read == [[1, 0, 0, 1], [0, 0, 0, 0], [0, 1, 0, 1]]


def _load(name):
    return parse_document(json.loads((EXAMPLES / name).read_text(encoding="utf-8")))


def test_real_examples_reproduce_matrix_row():
    graph = merge_graphs(
        [_load("curriculum_v2.json"), _load("rpd_v2.json"), _load("ckmv_v2.json")],
        program_id=PID,
    )
    matrix = derive_matrix(graph)

    row = next(r for r in matrix.rows if r.discipline == "Математический анализ I")
    ones = {
        code for code, value in zip(matrix.competency_codes, row.values, strict=True) if value
    }
    # план и РПД называют одну компетенцию по-разному: в примерах это ПКП-УНК-03
    assert ones == {"УК-1", "ПКП-УНК-03", "ОПК-1", "ПКА-1"}
    assert row.semester == 1
    assert row.credits == 6

    # реальная матрица заказчика на той же дисциплине — те же единицы,
    # но код УНК-03 (код-алиасинг между документами — работа анализатора)
    real = json.loads((EXAMPLES / "matrix_v2.json").read_text(encoding="utf-8"))
    real_ones = {
        cell["competency_code"] for cell in real["matrix_cells"]
        if cell["discipline_name"] == "Математический анализ I" and cell["value"] == 1
    }
    assert real_ones == {"УК-1", "УНК-03", "ОПК-1", "ПКА-1"}

    # входная матрица, если всё же загружена, вливается в те же рёбра «формирует»
    # и добавляет свою колонку с единицей — проекция остаётся честной к графу
    with_matrix = merge_graphs(
        [_load("curriculum_v2.json"), _load("rpd_v2.json"), _load("matrix_v2.json")],
        program_id=PID,
    )
    row2 = next(
        r for r in derive_matrix(with_matrix).rows if r.discipline == "Математический анализ I"
    )
    codes2 = derive_matrix(with_matrix).competency_codes
    ones2 = {code for code, value in zip(codes2, row2.values, strict=True) if value}
    assert ones2 == {"УК-1", "ПКП-УНК-03", "ОПК-1", "ПКА-1", "УНК-03"}
