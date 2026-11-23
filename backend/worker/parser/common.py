"""Общие операции: текст, числа, заголовки и чтение разделов Word."""

import re
from decimal import Decimal

from docx.oxml.ns import qn
from docx.table import Table
from docx.text.paragraph import Paragraph

from backend.contracts.document_v2.models import ParseIssue, SourceRef


def normalize(text):
    """Подготовить текст для поиска."""
    return " ".join(text.casefold().split())


def normalize_competency_code(code: str) -> str:
    """Убрать порядковый номер ЦКМВ/характеристики из кода компетенции.

    ЦКМВ и характеристика нумеруют компетенции сквозным порядком и вставляют
    номер в код (``ПКП-17-ППК-ОС4``), а учебный план и РПД используют код без
    него (``ПКП-ППК-ОС4``). Номер убирается только когда он стоит отдельным
    сегментом сразу после префикса и за ним идёт буквенный сегмент, поэтому
    обычные коды вида ``УК-1`` или ``ПКП-МО-1`` не меняются.
    """
    parts = code.split("-")
    if (
        len(parts) >= 3
        and parts[1].isdigit()
        and parts[2][:1].isalpha()
        and parts[2][:1].isupper()
    ):
        return _normalize_universal_code("-".join(parts[:1] + parts[2:]))
    return _normalize_universal_code(code)


def _normalize_universal_code(code: str) -> str:
    """Выровнять код универсальной компетенции: УНК-3 и УНК-03 — один код.

    Учебный план и РПД пишут ``ПКП-УНК-03``, ЦКМВ — ``УНК-03``, отдельные РПД —
    ``ПКП-УНК-3``. Это одна и та же компетенция, поэтому код приводится к
    ``ПКП-УНК-NN`` с выровненным двузначным номером.
    """
    match = re.fullmatch(r"(?:ПКП-)?УНК-(\d{1,2})", code)
    if match is None:
        return code
    return f"ПКП-УНК-{int(match.group(1)):02d}"


normalize_universal_code = _normalize_universal_code


def normalize_indicator_code(code: str) -> str:
    """Нормализовать код индикатора, сохранив числовые суффиксы."""
    base, *suffixes = code.split(".")
    normalized = normalize_competency_code(base)
    return ".".join([normalized, *suffixes])


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

    for name in type(output).model_fields:
        value = getattr(output, name)
        records = value if isinstance(value, list) else [value]
        for record in records:
            ref = getattr(record, "source_ref", None)
            if ref is None or ref.paragraph_index is not None:
                continue
            if ref.table is not None:
                ref.paragraph_index = table_anchor(ref)
            elif ref.section in headings:
                candidates = headings[ref.section]
                if len(candidates) == 1:
                    ref.paragraph_index = indices[candidates[0]]

    seen_issues = set()
    unique_issues = []

    for issue in output.parse_issues:
        ref = issue.source_ref
        key = (
            issue.message,
            issue.severity,
            ref.file if ref else None,
            ref.table if ref else None,
            ref.row if ref else None,
            ref.col if ref else None,
            ref.paragraph_index if ref else None,
        )

        if key in seen_issues:
            continue
        seen_issues.add(key)
        unique_issues.append(issue)

    output.parse_issues = unique_issues
    return type(output).model_validate(output.model_dump())


def read_section(
    document, file_name, is_heading, section_name,
    keep_numbered_items=False, include_headings=False, is_boundary=None,
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

    def heading_key(index):
        return re.sub(
            r"^(\d+(?:\.\d+)*)(?:\.\s*|\s+)(?=[^\d\s])",
            r"\1 ",
            normalize(blocks[index].text),
        )

    ambiguous = any(
        heading_key(previous) != heading_key(current)
        or any(
            not isinstance(block, Paragraph)
            or block.text.strip()
            or block._p.xpath(".//w:drawing | .//w:pict | .//w:object | .//w:sym | .//m:oMath")
            for block in blocks[previous + 1:current]
        )
        for previous, current in zip(starts, starts[1:])
    )

    if not starts or ambiguous:
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
    has_content = False
    if include_headings:
        result.append((
            blocks[start],
            SourceRef(
                file=file_name,
                section=section,
                paragraph_index=indices[blocks[start]._p],
            ),
        ))

    for block in blocks[starts[-1] + 1:]:
        if isinstance(block, Paragraph):
            boundary = is_boundary(block) if is_boundary else None
            if boundary is True:
                break
            current_level = level(block)
            if (
                keep_numbered_items
                and current_level == 1
                and (
                    (
                        not normalize(block.text).startswith("раздел ")
                        and not re.fullmatch(
                            r"(?:heading|заголовок)\s*\d+",
                            block.style.name,
                            re.IGNORECASE,
                        )
                    )
                    or re.fullmatch(
                        r"(?:задание|задача|вопрос|билет)\s*№?\s*\d+\s*[.):]?",
                        normalize(block.text),
                        re.IGNORECASE,
                    )
                )
            ):
                current_level = None

            if current_level is not None:
                if boundary is None and (
                    current_level <= depth or not is_subsection(root_section, block.text)
                ):
                    break

                section = block.text.strip()
                if include_headings:
                    has_content = True
                    result.append((
                        block,
                        SourceRef(
                            file=file_name,
                            section=section,
                            paragraph_index=indices[block._p],
                        ),
                    ))
                continue

            if block.text.strip() or (
                include_headings
                and block._p.xpath(
                    ".//w:drawing | .//w:pict | .//w:object | .//w:sym | .//m:oMath"
                )
            ):
                has_content = True
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
            has_content = True
            result.append((
                block,
                SourceRef(
                    file=file_name,
                    section=section,
                    table=table_number,
                ),
            ))

    if not has_content:
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
