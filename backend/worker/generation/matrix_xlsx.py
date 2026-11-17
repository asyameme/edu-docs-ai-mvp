"""Производная матрица компетенций: проекция графа программы в XLSX.

Матрица — производный документ (встреча с ментором 05.11): она не
разбирается из входного пакета, а вычисляется из сущностей и связей графа
и поэтому всегда согласована с текущим состоянием программы после правок.

Раскладка повторяет реальный образец заказчика
(tests/originals/Матрица компетенций.xlsx): лист «Матрица покрытия»,
строки — записи учебного плана (дисциплина × семестр), колонки —
компетенции, значение ячейки 0/1. Ячейка равна 1, если в графе есть связь
«формирует» от записи плана или от дисциплины с тем же названием;
семестр ребра учитывается, когда задан (ребро плана без семестра
действует на все семестры дисциплины).

Колонки метаданных заполняются только тем, что есть в графе: «Профиль»
остаётся пустым (в учебном плане такого поля нет), «Часть» получает код
блока из плана как есть, «№ строки» — порядковый номер строки матрицы.
Коды компетенций берутся из графа без сопоставления псевдонимов между
документами (например «УНК-03» в матрице заказчика против «ПКП-УНК-03»
в плане): сведение имён — задача анализатора, проекция его не выполняет.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from io import BytesIO

from backend.contracts.document_v2.graph import EducationalGraph, GraphNode
from openpyxl import Workbook, load_workbook
from openpyxl.styles import Alignment, Font

FORMS_RELATION = "формирует"
SHEET_NAME = "Матрица покрытия"

_HEADER_META = ("№ строки", "Семестр", "Профиль", "Часть", "ЗЕТ", "Дисциплина")
_DISCIPLINE_COLUMN = len(_HEADER_META)

# Порядок групп компетенций как в образце заказчика; незнакомые группы
# идут после известных по алфавиту.
_GROUP_ORDER = ("УК", "УНК", "УКБ", "ОПК", "ПКА", "ПКП")

_CODE_SPLIT = re.compile(r"[-_]")


@dataclass(frozen=True, slots=True)
class MatrixRow:
    """Строка матрицы: запись учебного плана или дисциплина без плана."""

    discipline: str
    semester: int | None
    part: str | None
    credits: int | None
    values: tuple[int, ...]


@dataclass(frozen=True, slots=True)
class DerivedMatrix:
    """Проекция графа: компетенции-колонки и строки со значениями 0/1."""

    competency_codes: tuple[str, ...]
    rows: tuple[MatrixRow, ...]


def _node_key(node_id: str) -> str:
    """Ключ сущности из её идентификатора ``программа|тип|ключ``."""

    return node_id.rsplit("|", 1)[-1]


def _competency_group(code: str) -> str:
    match = re.match(r"[A-ZА-ЯЁ]+", code)
    return match.group(0) if match else code


def _natural_code_key(code: str) -> tuple:
    group = _competency_group(code)
    group_rank = _GROUP_ORDER.index(group) if group in _GROUP_ORDER else len(_GROUP_ORDER)
    parts = []
    for part in _CODE_SPLIT.split(code[len(group) :]):
        parts.append((0, int(part), "") if part.isdigit() else (1, 0, part.casefold()))
    return (group_rank, group, tuple(parts))


def _source_row(node: GraphNode) -> int | None:
    rows = [ref.row for ref in node.source_refs if ref.row is not None]
    return min(rows) if rows else None


def _curriculum_rows(graph: EducationalGraph) -> list[dict]:
    entries = []
    for node in graph.nodes:
        if node.entity_type != "curriculum_item":
            continue
        key = _node_key(node.id)
        name, _, semester_raw = key.rpartition("::semester=")
        if not name:
            name = key
        try:
            semester = int(semester_raw) if semester_raw else None
        except ValueError:
            semester = None
        source_row = _source_row(node)
        order = (
            semester is None,
            semester or 0,
            source_row is None,
            source_row or 0,
            name,
        )
        entries.append({"order": order, "name": name, "semester": semester,
                        "part": node.attributes.get("block"),
                        "credits": node.attributes.get("credit_units")})
    entries.sort(key=lambda entry: entry["order"])
    return entries


def _discipline_rows(graph: EducationalGraph) -> list[dict]:
    entries = [
        {"order": (0, 0, 0, 0, _node_key(node.id)), "name": _node_key(node.id),
         "semester": None, "part": node.attributes.get("block"),
         "credits": node.attributes.get("credit_units")}
        for node in graph.nodes if node.entity_type == "discipline"
    ]
    entries.sort(key=lambda entry: entry["order"])
    return entries


def _formed_pairs(graph: EducationalGraph) -> tuple[tuple[str, int | None, str], ...]:
    """Пары (дисциплина, семестр ребра|None, компетенция) по связям «формирует»."""

    by_id = {node.id: node for node in graph.nodes}
    pairs: list[tuple[str, int | None, str]] = []
    for edge in graph.edges:
        if edge.relation_type != FORMS_RELATION:
            continue
        source = by_id.get(edge.source_id)
        # у ребра из to_graph оба конца всегда имеют узлы; тип из id — страховка
        entity_type = source.entity_type if source is not None else edge.source_id.split("|")[-2]
        if entity_type not in {"curriculum_item", "discipline"}:
            continue
        name = _node_key(edge.source_id)
        semester = edge.semester
        if entity_type == "curriculum_item":
            # запись плана семестровая: семестр — часть её идентификатора
            base, _, semester_raw = name.rpartition("::semester=")
            if base:
                name = base
                try:
                    semester = int(semester_raw) if semester_raw else semester
                except ValueError:
                    pass
        pairs.append((name, semester, _node_key(edge.target_id)))
    return tuple(pairs)


def derive_matrix(graph: EducationalGraph) -> DerivedMatrix:
    """Собрать производную матрицу из графа программы."""

    codes = tuple(
        sorted((_node_key(node.id) for node in graph.nodes if node.entity_type == "competency"),
               key=_natural_code_key),
    )
    if not codes:
        raise ValueError("Граф не содержит компетенций: матрица не может быть построена")

    entries = _curriculum_rows(graph) or _discipline_rows(graph)
    if not entries:
        raise ValueError("Граф не содержит записей учебного плана или дисциплин для строк матрицы")

    pairs = _formed_pairs(graph)
    rows = tuple(
        MatrixRow(
            discipline=entry["name"],
            semester=entry["semester"],
            part=entry["part"],
            credits=entry["credits"],
            values=tuple(_cell(entry["name"], entry["semester"], code, pairs) for code in codes),
        )
        for entry in entries
    )
    return DerivedMatrix(competency_codes=codes, rows=rows)


def _cell(discipline: str, semester: int | None, code: str,
          pairs: tuple[tuple[str, int | None, str], ...]) -> int:
    for name, edge_semester, competency in pairs:
        if name != discipline or competency != code:
            continue
        # ребро без семестра действует на дисциплину в целом; строка без
        # семестра (нет записей плана) — тоже дисциплина в целом
        if edge_semester is None or semester is None or edge_semester == semester:
            return 1
    return 0


def build_matrix_xlsx(source: DerivedMatrix | EducationalGraph) -> bytes:
    """Собрать XLSX производной матрицы в раскладке образца заказчика."""

    matrix = source if isinstance(source, DerivedMatrix) else derive_matrix(source)

    workbook = Workbook()
    sheet = workbook.active
    sheet.title = SHEET_NAME

    sheet.cell(row=1, column=1, value="Тип компетенции →")
    for offset, code in enumerate(matrix.competency_codes):
        sheet.cell(row=1, column=_DISCIPLINE_COLUMN + 1 + offset,
                   value=_competency_group(code))

    headers = [*_HEADER_META, *matrix.competency_codes]
    for column, title in enumerate(headers, start=1):
        sheet.cell(row=2, column=column, value=title)

    for index, row in enumerate(matrix.rows, start=3):
        sheet.cell(row=index, column=1, value=index - 2)
        sheet.cell(row=index, column=2, value=row.semester)
        sheet.cell(row=index, column=4, value=row.part)
        sheet.cell(row=index, column=5, value=row.credits)
        sheet.cell(row=index, column=6, value=row.discipline)
        for offset, value in enumerate(row.values):
            sheet.cell(row=index, column=_DISCIPLINE_COLUMN + 1 + offset, value=value)

    bold = Font(bold=True)
    for column in range(1, len(headers) + 1):
        for row in (1, 2):
            sheet.cell(row=row, column=column).font = bold
            sheet.cell(row=row, column=column).alignment = Alignment(horizontal="center")
    for column, width in ((1, 9), (2, 9), (3, 10), (4, 12), (5, 7), (6, 50)):
        sheet.column_dimensions[sheet.cell(row=2, column=column).column_letter].width = width
    for offset in range(len(matrix.competency_codes)):
        letter = sheet.cell(row=2, column=_DISCIPLINE_COLUMN + 1 + offset).column_letter
        sheet.column_dimensions[letter].width = 11
    sheet.freeze_panes = sheet.cell(row=3, column=_DISCIPLINE_COLUMN + 1)

    buffer = BytesIO()
    workbook.save(buffer)
    return buffer.getvalue()


def read_matrix_xlsx(content: bytes) -> tuple[tuple[str, ...], list[list[int]]]:
    """Прочитать значения 0/1 из собранной матрицы (для проверок и тестов)."""

    sheet = load_workbook(BytesIO(content), read_only=True)[SHEET_NAME]
    codes = tuple(
        str(cell.value) for cell in sheet[2][_DISCIPLINE_COLUMN:] if cell.value is not None
    )
    values = [
        [int(cell.value or 0) for cell in row[_DISCIPLINE_COLUMN:len(codes) + _DISCIPLINE_COLUMN]]
        for row in sheet.iter_rows(min_row=3)
        if row[_DISCIPLINE_COLUMN - 1].value is not None
    ]
    return codes, values
