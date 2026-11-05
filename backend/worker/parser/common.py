"""Общие операции: текст, числа, заголовки и чтение разделов Word."""

import re
from decimal import Decimal

from docx.oxml.ns import qn
from docx.table import Table
from docx.text.paragraph import Paragraph

from backend.contracts.parser_output import (
    ParseIssue,
    ParserOutput,
    SourceRef,
)


def normalize(text):
    """Подготовить текст для поиска."""
    return " ".join(text.casefold().split())


def heading_level(paragraph):
    """Определить уровень заголовка по номеру."""
    text = paragraph.text.strip()

    if re.match(
        r"^раздел\s+\d+(?:[.\s]|$)",
        normalize(text),
    ):
        return 1

    match = re.match(
        r"^(\d+(?:\.\d+)*)(?:\.\s*|\s+)([^\d\s].*)$",
        text,
    )

    if match:
        return len(match.group(1).split("."))

    return None


def is_subsection(parent, child):
    """Уточнить границу раздела при пропущенном промежуточном заголовке."""
    pattern = r"^(?:раздел\s+)?(\d+(?:\.\d+)*)"
    parent_number = re.match(pattern, normalize(parent))
    child_number = re.match(pattern, normalize(child))
    if parent_number is None or child_number is None:
        return True
    return child_number.group(1).startswith(parent_number.group(1) + ".")


def parse_workload_number(raw):
    """Прочитать число без округления."""
    text = raw.strip().replace("\u00a0", " ").replace("\u202f", " ")

    if not re.fullmatch(
        r"(?:\d+|\d{1,3}(?: \d{3})+)(?:[.,]\d+)?", text,
    ):
        raise ValueError(f"Неподдерживаемое число: {raw!r}")

    return Decimal(text.replace(" ", "").replace(",", "."))


def paragraph_indices(document):
    """Нумеровать физические абзацы тела, включая пустые и вложенные таблицы.

    Обход XML не дублирует абзацы объединенных ячеек и не включает
    колонтитулы, сноски и другие части файла.
    """
    return {
        paragraph: index
        for index, paragraph in enumerate(
            document.element.body.iter(qn("w:p")), 1,
        )
    }


def finalize_output(output, document):
    """Дополнить ссылки номерами абзацев и проверить общий контракт."""
    indices = paragraph_indices(document)
    tables = list(document.tables)
    anchors = {}
    headings = {}
    for paragraph in document.paragraphs:
        headings.setdefault(paragraph.text.strip(), []).append(paragraph._p)

    def table_anchor(ref):
        key = ref.table, ref.row, ref.col
        if key in anchors:
            return anchors[key]
        if not 1 <= ref.table <= len(tables):
            return None
        table = tables[ref.table - 1]
        element = table._tbl
        if ref.row is not None:
            if not 1 <= ref.row <= len(table.rows):
                return None
            row = table.rows[ref.row - 1]
            element = row._tr
            if ref.col is not None:
                cells = row.cells
                if not 1 <= ref.col <= len(cells):
                    return None
                element = cells[ref.col - 1]._tc
        paragraphs = list(element.iter(qn("w:p")))
        # При ссылке на целую ячейку/строку якорь - первый непустой
        # абзац. Для пустой ячейки сохраняется ее первый пустой абзац.
        paragraph = next((
            item for item in paragraphs
            if "".join(node.text or "" for node in item.iter(qn("w:t"))).strip()
        ), paragraphs[0] if paragraphs else None)
        anchors[key] = indices.get(paragraph)
        return anchors[key]

    records = []
    if output.program is not None:
        records.append(output.program)
    for name in (
        "disciplines", "competencies", "indicators", "learning_outcomes",
        "assessment_tools", "curriculum_items", "course_content", "score_scale",
        "relations", "parse_issues",
    ):
        records.extend(getattr(output, name))
    for record in records:
        ref = record.source_ref
        if ref is None:
            continue
        if ref.table is not None:
            ref.paragraph_index = table_anchor(ref)
        elif ref.paragraph_index is None and ref.section in headings:
            candidates = headings[ref.section]
            if len(candidates) == 1:
                ref.paragraph_index = indices[candidates[0]]
    return ParserOutput.model_validate(output.model_dump())


def read_section(
    document, file_name, is_heading, section_name,
    keep_numbered_items=False,
):
    """Прочитать раздел в исходном порядке вместе с координатами."""
    blocks = list(document.iter_inner_content())
    indices = paragraph_indices(document)

    starts = [
        index
        for index, block in enumerate(blocks)
        if isinstance(block, Paragraph)
        and not block.style.name.casefold().startswith((
            "toc", "оглавление",
        ))
        and not re.search(r"\t\s*\d+\s*$", block.text)
        and is_heading(block)
    ]

    if len(starts) != 1:
        return [], [ParseIssue(
            category="not_found" if not starts else "ambiguous",
            message=(
                "Не удалось однозначно найти "
                f"раздел: {section_name}"
            ),
            source_ref=SourceRef(file=file_name),
        )]

    def level(paragraph):
        number = heading_level(paragraph)
        style = re.fullmatch(
            r"(?:heading|заголовок)\s*(\d+)",
            paragraph.style.name,
            re.IGNORECASE,
        )

        return number if number is not None else (
            int(style.group(1)) if style else None
        )

    start = starts[0]
    section = blocks[start].text.strip()
    root_section = section
    depth = level(blocks[start]) or 2

    table_number = sum(
        isinstance(block, Table)
        for block in blocks[:start]
    )
    result = []

    for block in blocks[start + 1:]:
        if isinstance(block, Paragraph):
            current_level = level(block)
            if (
                keep_numbered_items
                and current_level == 1
                and not normalize(block.text).startswith("раздел ")
                and not re.fullmatch(
                    r"(?:heading|заголовок)\s*\d+",
                    block.style.name,
                    re.IGNORECASE,
                )
            ):
                current_level = None

            if current_level is not None:
                if current_level <= depth or not is_subsection(root_section, block.text):
                    break

                section = block.text.strip()
                continue

            if block.text.strip():
                result.append((
                    block,
                    SourceRef(
                        file=file_name,
                        section=section,
                        paragraph_index=indices[block._p],
                    ),
                ))

        elif isinstance(block, Table):
            table_number += 1
            result.append((
                block,
                SourceRef(
                    file=file_name,
                    section=section,
                    table=table_number,
                ),
            ))

    if not result:
        return [], [ParseIssue(
            category="empty_cell",
            message=f"Раздел пуст: {section_name}",
            source_ref=SourceRef(
                file=file_name,
                section=blocks[start].text.strip(),
                paragraph_index=indices[blocks[start]._p],
            ),
        )]

    return result, []
