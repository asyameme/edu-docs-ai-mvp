"""Учебный план DOCX: справочник компетенций и строки по семестрам."""

import argparse
import re
from collections import defaultdict
from hashlib import sha256
from io import BytesIO
from pathlib import Path

from docx import Document
from docx.table import Table
from docx.text.paragraph import Paragraph

from .common import heading_level, normalize, parse_workload_number, read_section
from .models import (
    Competency, CurriculumItem, ParseIssue, ParserMeta, ParserOutput,
    Relation, SourceRef,
)
from .results import split_competency


def _label(text):
    return normalize(text).replace("\u0451", "е").rstrip(".:")


def _heading(paragraph, titles):
    text = re.sub(
        r"^(?:раздел\s+)?\d+(?:\.\d+)*\.?\s*",
        "", _label(paragraph.text),
    )
    return text in titles


def _issue(output, category, message, ref):
    output.parse_issues.append(ParseIssue(
        category=category, message=message, source_ref=ref,
    ))


def _ref(table_ref, row, column=None):
    return table_ref.model_copy(update={"row": row, "col": column})


def _integer(output, raw, field, ref, minimum=0):
    if not raw.strip():
        _issue(output, "empty_cell", f"УП: пустое поле {field}", ref)
        return None
    try:
        value = parse_workload_number(raw)
    except ValueError:
        value = None
    if value is None or value != value.to_integral_value() or value < minimum:
        _issue(
            output, "unexpected_format",
            f"УП: {field} = {raw!r} не соответствует целому числу >= {minimum}", ref,
        )
        return None
    return int(value)


def _catalog_header(row):
    columns = {}
    for index, cell in enumerate(row.cells):
        text = _label(cell.text)
        if text in {"код компетенции", "коды компетенций"}:
            columns["code"] = index
        elif "компетенци" in text and any(
            word in text for word in ("наименование", "описание", "формулировка")
        ):
            columns["formulation"] = index
    return columns if len(columns) == 2 else None


def extract_curriculum_competencies(output, document):
    blocks, issues = read_section(
        document, output.meta.file_name,
        lambda p: _heading(p, {"формируемые компетенции", "перечень компетенций"}),
        "формируемые компетенции учебного плана",
    )
    output.parse_issues.extend(issues)
    found = False
    for table, table_ref in blocks:
        if not isinstance(table, Table):
            continue
        columns = None
        seen = set()
        for number, row in enumerate(table.rows, 1):
            header = _catalog_header(row)
            if header is not None:
                columns = header
                found = True
                continue
            if columns is None:
                continue
            cells = row.cells
            if max(columns.values()) >= len(cells) or row.grid_cols_before or row.grid_cols_after:
                _issue(
                    output, "unexpected_format",
                    "УП: строка справочника не совпадает с шапкой",
                    _ref(table_ref, number),
                )
                continue
            code_cell, text_cell = (cells[columns[key]] for key in ("code", "formulation"))
            if not code_cell.text.strip() and not text_cell.text.strip():
                continue
            key = code_cell._tc, text_cell._tc
            if key in seen:
                continue
            seen.add(key)
            ref = _ref(table_ref, number, columns["code"] + 1)
            parsed = split_competency(code_cell.text)
            if parsed is None or parsed[1]:
                _issue(output, "unparsed_row", f"УП: не распознан код компетенции {code_cell.text!r}", ref)
                continue
            formulation = " ".join(text_cell.text.split())
            if not formulation:
                _issue(
                    output, "empty_cell", f"УП: нет описания компетенции {parsed[0]}",
                    _ref(table_ref, number, columns["formulation"] + 1),
                )
                continue
            output.competencies.append(Competency(
                code=parsed[0], formulation=formulation, source_ref=ref,
            ))
    if blocks and not found:
        _issue(output, "not_found", "УП: не найдена таблица кодов и описаний компетенций", blocks[0][1])
    elif found and not output.competencies:
        _issue(output, "empty_cell", "УП: из справочника не извлечено ни одной компетенции", blocks[0][1])


def _identity_columns(row):
    columns = {}
    for index, cell in enumerate(row.cells):
        text = _label(cell.text)
        if text in {"код блока", "блок"}:
            key = "block"
        elif text == "зет" or "трудоемкость" in text and "зачетн" in text:
            key = "credits"
        elif text in {"код компетенции", "коды компетенций"}:
            key = "codes"
        elif text.startswith(("наименование дисциплины", "название дисциплины")):
            key = "name"
        elif text == "семестр":
            key = "semester"
        elif text.startswith((
            "виды текущего контроля", "форма промежуточной аттестации",
        )) or text == "формы контроля":
            key = "control"
        else:
            continue
        if key in columns:
            return None
        columns[key] = index
    return columns if {"block", "credits", "codes", "name"} <= columns.keys() else None


def _hour_kind(labels):
    """Выбрать независимые показатели известной шапки, исключая справочные."""
    text = " / ".join(labels)
    leaf = labels[-1] if labels else ""
    if "активн" in text or "интерактивн" in text:
        return "reference"
    group = any(word in text for word in ("аудиторн", "контактн", "самостоятельн"))
    if re.fullmatch(r"(?:всего|итого|общее количество|общий объем)(?:\s+часов)?", leaf):
        return "subtotal" if group else "total"
    if leaf in {"всего часов", "часы всего", "общее количество часов", "общая трудоемкость, часов"}:
        return "subtotal" if group else "total"
    if re.fullmatch(
        r"(?:лекции|семинары|консультации|практические занятия|лабораторные работы|"
        r"контрольные работы|коллоквиум|текущий контроль|промежуточная аттестация|"
        r"коллоквиумы|под руководством преподавателя|в присутствии преподавателя)"
        r"(?:,?\s+(?:часов|часы))?", leaf,
    ):
        return "component"
    # В проверенном шаблоне СПбГУ это самостоятельная колонка работы,
    # соседние колонки не являются ее числовым итогом.
    if "самостоятельн" in text and re.match(r"в т\.?\s*ч\.? с использованием учебно-методич", leaf):
        return "component"
    if re.match(r"самостоятельная работа(?:,?\s+(?:часов|часы))?$", leaf):
        return "component"
    return "unknown"


def _curriculum_header(rows, start):
    columns = _identity_columns(rows[start])
    if columns is None:
        return None
    width = len(rows[start].cells)
    labels = [[] for _ in range(width)]
    end = start
    for index in range(start, min(start + 5, len(rows))):
        row = rows[index]
        if len(row.cells) != width or _identity_columns(row) != columns:
            break
        end = index
        for column, cell in enumerate(row.cells):
            value = _label(cell.text)
            if value and (not labels[column] or value != labels[column][-1]):
                labels[column].append(value)
    hours = {
        column: _hour_kind(path)
        for column, path in enumerate(labels)
        if column not in columns.values()
    }
    return {"columns": columns, "hours": hours, "end": end, "width": width}


def _hours_total(output, cells, header, ref):
    totals = [col for col, kind in header["hours"].items() if kind == "total"]
    if len(totals) == 1:
        column = totals[0]
        return _integer(
            output, cells[column].text, "hours_total",
            ref.model_copy(update={"col": column + 1}),
        )
    if len(totals) > 1:
        _issue(output, "ambiguous", "УП: несколько колонок общего количества часов", ref)
        return None
    components = [col for col, kind in header["hours"].items() if kind == "component"]
    unsupported = [col for col, kind in header["hours"].items() if kind in {"unknown", "subtotal"}]
    if not components or unsupported:
        _issue(output, "unexpected_format", "УП: нельзя однозначно определить независимые колонки часов по шапке", ref)
        return None
    values = []
    seen = set()
    for column in components:
        cell = cells[column]
        cell_ref = ref.model_copy(update={"col": column + 1})
        if cell._tc in seen:
            _issue(output, "ambiguous", "УП: одна ячейка объединяет несколько видов нагрузки", cell_ref)
            return None
        seen.add(cell._tc)
        values.append(_integer(output, cell.text, "часы вида работы", cell_ref))
    return sum(values) if all(value is not None for value in values) else None


def _discipline_name(cell):
    parts = []
    for line in cell.text.splitlines():
        line = re.sub(r"^\s*\[\d+\]\s*", "", line)
        line = " ".join(line.split())
        if not line:
            continue
        if (
            parts and re.match(r"[A-Za-z]", line)
            and not re.search(r"[А-Яа-я\u0401\u0451]", line)
        ):
            break
        parts.append(line)
    return " ".join(parts)


def _add_relations(output, raw, name, ref):
    known = {item.code for item in output.competencies}
    if not raw.strip():
        _issue(output, "empty_cell", f"УП: нет кодов компетенций для {name!r}; связи не созданы", ref)
        return
    for token in re.split(r"[,;\s]+", raw.strip()):
        parsed = split_competency(token)
        if parsed is None or parsed[1]:
            _issue(output, "unparsed_row", f"УП: не распознан код в перечне компетенций {token!r}", ref)
        elif parsed[0] not in known:
            _issue(
                output, "manual_review",
                f"УП: код {parsed[0]} указан в строке, но не извлечен "
                "из справочника; связь не создана", ref,
            )
        else:
            output.relations.append(Relation(
                src_entity="curriculum_item", src_key=name,
                dst_entity="competency", dst_key=parsed[0],
                kind="формирует", source_ref=ref,
            ))


def extract_curriculum_items(output, document):
    blocks, issues = read_section(
        document, output.meta.file_name,
        lambda p: _heading(p, {
            "организация обучения и итоговой аттестации", "организация обучения",
        }),
        "организация обучения учебного плана",
    )
    output.parse_issues.extend(issues)
    semester = None
    semester_title = None
    context = {}
    found = False

    def caption(text, ref):
        nonlocal semester, semester_title
        label = _label(text)
        match = re.fullmatch(r"(?:[сc]\d+\.\s*)?семестр\s+(\d+)", label)
        if match:
            value = int(match.group(1))
            semester = value if 1 <= value <= 8 else None
            semester_title = text
            context.clear()
            if semester is None:
                _issue(output, "unexpected_format", f"УП: семестр {value} вне диапазона 1-8 текущего контракта", ref)
        elif re.match(r"(?:[сc]\d+\.\s*)?семестр\b", label):
            semester, semester_title = None, None
            context.clear()
            _issue(output, "ambiguous", f"УП: не распознана подпись семестра {text!r}", ref)
        elif re.fullmatch(r"\d+\s+год\s+обучения", label):
            semester, semester_title = None, None
            context.clear()
        elif label.startswith(("базовая часть", "вариативная часть", "факультативные")):
            context.clear()
            context["part"] = text
        elif label.startswith("блок"):
            context.pop("profile", None)
            context.pop("track", None)
            context["group"] = text
        elif label.startswith("профиль"):
            context.pop("track", None)
            context["profile"] = text
        elif label.startswith("траектория"):
            context["track"] = text

    for block, table_ref in blocks:
        if isinstance(block, Paragraph):
            caption(block.text.strip(), table_ref)
            continue
        rows = list(block.rows)
        header = None
        seen_groups = set()
        for index, row in enumerate(rows):
            cells = row.cells
            row_ref = _ref(table_ref, index + 1)
            if header is not None and index <= header["end"]:
                continue
            candidate = _curriculum_header(rows, index)
            if candidate is not None:
                header = candidate
                found = True
                continue
            if not cells or not any(cell.text.strip() for cell in cells):
                continue
            if len({cell._tc for cell in cells}) == 1:
                caption(" ".join(cells[0].text.split()), row_ref)
                continue
            if header is None:
                continue
            if len(cells) != header["width"] or row.grid_cols_before or row.grid_cols_after:
                _issue(output, "unexpected_format", "УП: строка нагрузки не совпадает с сеткой шапки", row_ref)
                continue
            columns = header["columns"]
            name_cell = cells[columns["name"]]
            name = _discipline_name(name_cell)
            row_ref = row_ref.model_copy(update={
                "section": "; ".join(filter(None, [table_ref.section, semester_title, *context.values()])),
                "col": columns["name"] + 1,
            })
            if re.fullmatch(r"(?:итого|всего)(?:\s+по\b.*|\s*[:.]?)", _label(name)):
                continue
            if _label(name).startswith("дисциплины по выбору"):
                if name_cell._tc not in seen_groups:
                    seen_groups.add(name_cell._tc)
                    _issue(
                        output, "manual_review",
                        "УП: группа выбора не является отдельной дисциплиной: "
                        f"{name_cell.text}; ЗЕТ: {cells[columns['credits']].text}", row_ref,
                    )
                continue
            if not name:
                _issue(output, "empty_cell", "УП: нет названия дисциплины или практики", row_ref)
                continue
            actual_semester = semester
            if "semester" in columns:
                col = columns["semester"]
                actual_semester = _integer(
                    output, cells[col].text, "semester",
                    row_ref.model_copy(update={"col": col + 1}), minimum=1,
                )
            if actual_semester is None or not 1 <= actual_semester <= 8:
                _issue(
                    output, "manual_review",
                    f"УП: для {name!r} не определен семестр 1-8; строка не передана", row_ref,
                )
                continue
            raw_block = " ".join(cells[columns["block"]].text.split())
            # Факультативы в исходнике не имеют кода блока, но явно
            # находятся под общей подписью "Факультативные занятия".
            part = context.get("part", "")
            if raw_block in {"", "-", "\u2013", "\u2014"} and _label(part).startswith("факультативные"):
                raw_block = part
            if raw_block in {"", "-", "\u2013", "\u2014"}:
                _issue(
                    output, "empty_cell", f"УП: не указан блок для {name!r}; строка не передана",
                    row_ref.model_copy(update={"col": columns["block"] + 1}),
                )
                continue
            credit_col = columns["credits"]
            credits = _integer(
                output, cells[credit_col].text, "credit_units",
                row_ref.model_copy(update={"col": credit_col + 1}), minimum=1,
            )
            hours = _hours_total(output, cells, header, row_ref)
            if credits is None or hours is None:
                continue
            output.curriculum_items.append(CurriculumItem(
                discipline_name=name, semester=actual_semester,
                credit_units=credits, hours_total=hours,
                block=raw_block, source_ref=row_ref,
            ))
            code_col = columns["codes"]
            _add_relations(
                output, cells[code_col].text, name,
                row_ref.model_copy(update={"col": code_col + 1}),
            )
        if header is None:
            _issue(output, "unexpected_format", "УП: таблица раздела обучения не имеет поддержанной шапки", table_ref)
    if blocks and not found:
        _issue(output, "not_found", "УП: не найдена таблица дисциплин по семестрам", blocks[0][1])
    elif found and not output.curriculum_items:
        _issue(output, "manual_review", "УП: из таблиц обучения не извлечено ни одной полной строки", blocks[0][1])

    by_name = defaultdict(list)
    for item in output.curriculum_items:
        by_name[item.discipline_name].append(item)
    for name, items in by_name.items():
        if len(items) > 1:
            _issue(
                output, "manual_review",
                f"УП: название {name!r} встречается в {len(items)} строках. "
                "Строки и их источники сохранены; контракт задает ключ связи "
                "только названием, без семестра и траектории", items[0].source_ref,
            )


def parse_curriculum(file_path):
    """Прочитать Word-план, не исправляя значения и не выбирая траекторию."""
    file_path = Path(file_path)
    if file_path.suffix.lower() != ".docx":
        raise ValueError("Ожидается учебный план в формате DOCX")
    raw = file_path.read_bytes()
    document = Document(BytesIO(raw))
    title_parts = []
    for block in document.iter_inner_content():
        if isinstance(block, Paragraph):
            if heading_level(block) is not None:
                break
            title_parts.append(block.text)
        elif isinstance(block, Table):
            title_parts.extend(cell.text for row in block.rows for cell in row.cells)
    title = re.sub(r"\s+", "", "".join(title_parts)).casefold()
    if "учебныйплан" not in title or "рабочаяпрограмма" in title:
        raise ValueError("Не удалось однозначно распознать учебный план по титулу")
    output = ParserOutput(meta=ParserMeta(
        doc_type="CURRICULUM", file_name=file_path.name,
        file_hash=sha256(raw).hexdigest(), parser_config="curriculum_docx_v1",
    ))
    extract_curriculum_competencies(output, document)
    extract_curriculum_items(output, document)
    return ParserOutput.model_validate(output.model_dump())


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("input", type=Path)
    parser.add_argument("-o", "--output", type=Path, required=True)
    args = parser.parse_args()
    if args.input.resolve() == args.output.resolve():
        parser.error("Выходной файл должен отличаться от исходного")
    try:
        output = parse_curriculum(args.input)
    except (ValueError, OSError) as error:
        parser.error(str(error))
    text = output.model_dump_json(indent=2)
    ParserOutput.model_validate_json(text)
    args.output.write_text(text, encoding="utf-8")
    print(
        f"Компетенций: {len(output.competencies)}. "
        f"Строк УП: {len(output.curriculum_items)}. "
        f"Связей: {len(output.relations)}. "
        f"Замечаний: {len(output.parse_issues)}. JSON: {args.output}"
    )


if __name__ == "__main__":
    main()
