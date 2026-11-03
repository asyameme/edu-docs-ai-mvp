"""Общие операции: текст, числа, заголовки и чтение разделов Word."""

import re
from decimal import Decimal

from docx.table import Table
from docx.text.paragraph import Paragraph

from .models import (
    ParseIssue,
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


def read_section(
    document, file_name, is_heading, section_name,
    keep_numbered_items=False,
):
    """Прочитать раздел в исходном порядке вместе с координатами."""
    blocks = list(document.iter_inner_content())

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
            ),
        )]

    return result, []
