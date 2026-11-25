"""Apply precise text changes to an existing DOCX while retaining its layout.

The generator starts from the original document and edits only a uniquely
identified text value. This follows the project decision to replace changed
entities in-place and leave the rest of each source document untouched.
"""

from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from difflib import SequenceMatcher
from io import BytesIO
from xml.sax.saxutils import quoteattr
from zipfile import ZIP_DEFLATED, BadZipFile, ZipFile

W_NS = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
W = f"{{{W_NS}}}"
DOCUMENT_PART = "word/document.xml"
_XML_SPACE = "{http://www.w3.org/XML/1998/namespace}space"
# В таблицах текст одного индикатора может идти сразу за кодом следующего.
_INDICATOR_CODE_PREFIX = re.compile(
    r"[А-ЯA-Z][А-ЯA-Z0-9]*(?:-[А-ЯA-Z0-9]+)+(?:\.\d+)+(?:$|[^А-ЯA-Z0-9.])"
)


class DocxPatchError(ValueError):
    """Raised when a DOCX cannot be changed safely and unambiguously."""


@dataclass(frozen=True, slots=True)
class DocxPatch:
    """Replace one old text value at its parser ``source_ref`` location.

    ``table``, ``row``, ``col`` and ``paragraph_index`` in ``source_ref`` are
    one-based. If the column is omitted, the old text must identify exactly
    one paragraph in the selected row. If no table is supplied,
    ``paragraph_index`` selects one body paragraph (numbered in document
    order, including paragraphs inside tables, without headers and footers);
    without it the old text must occur exactly once in the document body.
    ``section`` is retained as diagnostic context.
    """

    source_ref: Mapping[str, object]
    old_text: str
    new_text: str
    normalize_whitespace: bool = False


def _validate_docx(data: bytes) -> None:
    try:
        with ZipFile(BytesIO(data)) as package:
            names = set(package.namelist())
            if "[Content_Types].xml" not in names or DOCUMENT_PART not in names:
                raise DocxPatchError("Файл не содержит обязательные части DOCX")
    except BadZipFile as exc:
        raise DocxPatchError("Файл не является корректным DOCX/ZIP") from exc


def _positive_int(value: object, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise DocxPatchError(f"source_ref.{field} должен быть положительным целым числом")
    return value


def _resolve_paragraphs(root: ET.Element, source_ref: Mapping[str, object]) -> list[ET.Element]:
    table_value = source_ref.get("table")
    row_value = source_ref.get("row")
    col_value = source_ref.get("col")

    if table_value is None:
        if row_value is not None or col_value is not None:
            raise DocxPatchError("source_ref.row/col требуют source_ref.table")
        body = root.find(f"{W}body")
        if body is None:
            raise DocxPatchError("В DOCX не найдено тело документа")
        paragraphs = list(body.iter(f"{W}p"))
        index_value = source_ref.get("paragraph_index")
        if index_value is None:
            return paragraphs
        number = _positive_int(index_value, "paragraph_index")
        if number > len(paragraphs):
            raise DocxPatchError(
                f"Абзац #{number} из source_ref не найден; в документе {len(paragraphs)} абзацев"
            )
        return [paragraphs[number - 1]]

    table_number = _positive_int(table_value, "table")
    if col_value is not None and row_value is None:
        raise DocxPatchError("source_ref.col требует source_ref.row")

    body = root.find(f"{W}body")
    if body is None:
        raise DocxPatchError("В DOCX не найдено тело документа")
    tables = body.findall(f"{W}tbl")
    if table_number > len(tables):
        raise DocxPatchError(
            f"Таблица #{table_number} из source_ref не найдена; в документе {len(tables)} таблиц"
        )
    table = tables[table_number - 1]
    rows = table.findall(f"{W}tr")

    if row_value is None:
        selected_rows = rows
    else:
        row_number = _positive_int(row_value, "row")
        if row_number > len(rows):
            raise DocxPatchError(
                f"Строка #{row_number} из source_ref не найдена в таблице #{table_number}"
            )
        selected_rows = [rows[row_number - 1]]

    paragraphs: list[ET.Element] = []
    for row in selected_rows:
        cells = row.findall(f"{W}tc")
        if col_value is not None:
            col_number = _positive_int(col_value, "col")
            if col_number > len(cells):
                raise DocxPatchError(
                    f"Столбец #{col_number} из source_ref не найден в строке #{row_value}"
                )
            cells = [cells[col_number - 1]]
        for cell in cells:
            paragraphs.extend(cell.findall(f"{W}p"))
    return paragraphs


def _run_for_text_node(paragraph: ET.Element, target: ET.Element) -> ET.Element | None:
    for run in paragraph.iter(f"{W}r"):
        if any(node is target for node in run.iter(f"{W}t")):
            return run
    return None


# Язык фрагмента и отключённая проверка правописания вид текста не меняют.
_NON_VISUAL_PROPERTIES = {f"{W}lang", f"{W}noProof", f"{W}rPrChange"}


def _format_signature(run: ET.Element | None) -> tuple | None:
    """Видимое оформление фрагмента; None, когда сам фрагмент не найден."""
    if run is None:
        return None
    properties = run.find(f"{W}rPr")
    if properties is None:
        return ()
    return (
        tuple(sorted(properties.attrib.items())),
        tuple(
            (child.tag, tuple(sorted(child.attrib.items())))
            for child in properties
            if child.tag not in _NON_VISUAL_PROPERTIES
        ),
    )


def _set_text(node: ET.Element, value: str) -> None:
    node.text = value
    if value[:1].isspace() or value[-1:].isspace():
        node.set(_XML_SPACE, "preserve")
    else:
        node.attrib.pop(_XML_SPACE, None)


def _edit_in_place(text_nodes, values, spans, first, last, start, old_text, new_text, runs) -> int:
    """Внести локальную правку в текст с разным оформлением, не трогая остальное.

    Правка разбивается на неизменные и изменённые участки, и изменённые
    вносятся в те фрагменты, где они находились: пометку уровня можно удалить
    даже из фрагмента с другим шрифтом, а заменённое число сохраняет своё
    выделение. Однозначно оформить нельзя только новый текст на стыке
    по-разному оформленных фрагментов — такая замена отклоняется, как раньше.
    """

    edits: dict[int, list[tuple[int, int, str]]] = {}
    matcher = SequenceMatcher(None, old_text, new_text, autojunk=False)
    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        if tag == "equal":
            continue
        left, right = start + i1, start + i2
        inserted = new_text[j1:j2]
        if tag == "insert":
            # Вставка на стыке относится к левому фрагменту, к началу — к первому.
            owners = [k for k in range(first, last + 1) if spans[k][0] < left <= spans[k][1]]
            owner = owners[0] if owners else first
            position = left - spans[owner][0]
            edits.setdefault(owner, []).append((position, position, inserted))
            continue
        owners = [k for k in range(first, last + 1) if spans[k][0] < right and left < spans[k][1]]
        if inserted and len({_format_signature(runs[k - first]) for k in owners}) > 1:
            raise DocxPatchError(
                "Искомый текст пересекает фрагменты с разным форматированием; "
                "автоматическая замена может изменить оформление"
            )
        for k in owners:
            local_start = max(left, spans[k][0]) - spans[k][0]
            local_end = min(right, spans[k][1]) - spans[k][0]
            edits.setdefault(k, []).append(
                (local_start, local_end, inserted if k == owners[0] else "")
            )
    for k, changes in edits.items():
        value = values[k]
        for local_start, local_end, text in sorted(changes, reverse=True):
            value = value[:local_start] + text + value[local_end:]
        _set_text(text_nodes[k], value)
    return 1


def _replace_in_paragraph(paragraph: ET.Element, old_text: str, new_text: str) -> int:
    text_nodes = list(paragraph.iter(f"{W}t"))
    if not text_nodes:
        return 0

    values = [node.text or "" for node in text_nodes]
    combined = "".join(values)
    occurrences = combined.count(old_text)
    if occurrences == 0:
        return 0
    if occurrences != 1:
        raise DocxPatchError("Искомый текст повторяется в одном абзаце")

    start = combined.index(old_text)
    end = start + len(old_text)
    spans: list[tuple[int, int]] = []
    offset = 0
    for value in values:
        spans.append((offset, offset + len(value)))
        offset += len(value)

    first = next(i for i, (left, right) in enumerate(spans) if left <= start < right)
    last = next(i for i, (left, right) in enumerate(spans) if left < end <= right)
    if first == last:
        left, _ = spans[first]
        prefix = values[first][: start - left]
        suffix = values[first][end - left :]
        _set_text(text_nodes[first], prefix + new_text + suffix)
        return 1

    structural_breaks = (
        f"{W}tab",
        f"{W}br",
        f"{W}cr",
        f"{W}fldChar",
        f"{W}instrText",
        f"{W}hyperlink",
    )
    if any(any(paragraph.iter(tag)) for tag in structural_breaks):
        raise DocxPatchError("Искомый текст пересекает разрыв, поле или гиперссылку")

    involved_runs = [_run_for_text_node(paragraph, text_nodes[i]) for i in range(first, last + 1)]
    if any(run is None for run in involved_runs):
        raise DocxPatchError("Не удалось безопасно определить форматирование текста")
    if len({_format_signature(run) for run in involved_runs}) > 1:
        return _edit_in_place(
            text_nodes, values, spans, first, last, start, old_text, new_text, involved_runs
        )

    left, _ = spans[first]
    _set_text(text_nodes[first], values[first][: start - left] + new_text)
    for index in range(first + 1, last):
        _set_text(text_nodes[index], "")
    _, right = spans[last]
    _set_text(text_nodes[last], values[last][end - (right - len(values[last])) :])
    return 1


def _rewrite_document_xml(document: bytes, transform) -> bytes:
    """Return a DOCX copy with ``transform`` applied to ``word/document.xml``.

    Only that part is rewritten; all other package parts keep their contents.
    The input byte string is never modified.
    """

    source = ZipFile(BytesIO(document), "r")
    try:
        xml = source.read(DOCUMENT_PART)
        try:
            root = ET.fromstring(xml)
        except ET.ParseError as exc:
            raise DocxPatchError("Не удалось прочитать word/document.xml") from exc

        namespace_declarations: dict[str, str] = {}
        for _, (prefix, uri) in ET.iterparse(BytesIO(xml), events=("start-ns",)):
            namespace_declarations[prefix] = uri
            if not prefix.startswith("ns"):
                try:
                    ET.register_namespace(prefix, uri)
                except ValueError:
                    continue

        transform(root)

        updated_xml = ET.tostring(root, encoding="utf-8", xml_declaration=True)
        declaration_end = updated_xml.find(b"?>")
        root_start = updated_xml.find(b"<", declaration_end + 2)
        root_end = updated_xml.find(b">", root_start)
        if root_start < 0 or root_end < 0:
            raise DocxPatchError("Не удалось сериализовать word/document.xml")
        root_tag = updated_xml[root_start:root_end]
        declarations = bytearray()
        for prefix, uri in namespace_declarations.items():
            if prefix == "xml":
                continue
            name = "xmlns" if not prefix else f"xmlns:{prefix}"
            if re.search(rb"(?:^|\s)" + re.escape(name.encode()) + rb"=", root_tag) is None:
                declarations.extend(f" {name}=".encode("ascii"))
                declarations.extend(quoteattr(uri).encode("utf-8"))
        if declarations:
            updated_xml = updated_xml[:root_end] + declarations + updated_xml[root_end:]
        output = BytesIO()
        with ZipFile(output, "w", compression=ZIP_DEFLATED) as target:
            for info in source.infolist():
                if info.filename == DOCUMENT_PART:
                    payload = updated_xml
                else:
                    payload = source.read(info.filename)
                target.writestr(info, payload)
        return output.getvalue()
    finally:
        source.close()


def _paragraph_text(paragraph: ET.Element) -> str:
    return "".join(node.text or "" for node in paragraph.iter(f"{W}t"))


def _text_matches(root, source_ref, old_text, normalize_whitespace):
    paragraphs = _resolve_paragraphs(root, source_ref)
    if not normalize_whitespace:
        return [
            [(paragraph, old_text)]
            for paragraph in paragraphs
            for _ in range(_paragraph_text(paragraph).count(old_text))
        ]
    words = old_text.split()
    if not words:
        return []
    pattern = re.compile(r"\s+".join(map(re.escape, words)))
    # A formulation can span paragraphs of one table cell. Never search across
    # cells or across unrelated body paragraphs.
    parent_groups = []
    if source_ref.get("col") is not None or len(paragraphs) < 2:
        groups = [paragraphs]
    else:
        selected = set(paragraphs)
        parents = {
            p: cell for cell in root.iter(f"{W}tc")
            for p in cell.findall(f"{W}p") if p in selected
        }
        for paragraph in paragraphs:
            parent = parents.get(paragraph, paragraph)
            if not parent_groups or parent is not parent_groups[-1][0]:
                parent_groups.append((parent, []))
            parent_groups[-1][1].append(paragraph)
        groups = [group for _, group in parent_groups]
    matches = []
    for group in groups:
        spans = []
        position = 0
        for paragraph in group:
            value = _paragraph_text(paragraph)
            spans.append((paragraph, value, position))
            position += len(value) + 1
        joined = "\n".join(value for _, value, _ in spans)
        for hit in pattern.finditer(joined):
            # Совпадение после нормализации должно сохранять границы полного слова.
            # Буква рядом с буквой обычно означает фрагмент внутри слова; цифра
            # рядом с буквой допустима для склеенного кода «УК-1.1Текст».
            # В ячейке следующий код индикатора может примыкать к полной
            # формулировке без пробела.
            right_suffix = joined[hit.end():]
            right_is_indicator_code = bool(_INDICATOR_CODE_PREFIX.match(right_suffix))
            if (hit.start() and words[0][0].isalpha() and joined[hit.start() - 1].isalpha()
                    or hit.end() < len(joined) and words[-1][-1].isalpha()
                    and joined[hit.end()].isalpha() and not right_is_indicator_code):
                continue
            matches.append([
                (paragraph, value[max(0, hit.start() - start):hit.end() - start])
                for paragraph, value, start in spans
                if start < hit.end() and start + len(value) > hit.start()
            ])
    return matches


def count_text_matches(document, source_ref, old_text, *, normalize_whitespace=False) -> int:
    return len(_text_matches(_document_root(document), source_ref, old_text, normalize_whitespace))


def _replace_text_match(parts, new_text):
    if len(parts) == 1:
        paragraph, original = parts[0]
        return _replace_in_paragraph(paragraph, original, new_text)
    # Keep the existing paragraph boundaries. Align them with the unchanged
    # words rather than combining the runs or deleting paragraph formatting.
    old = [word for _, value in parts for word in value.split()]
    new = new_text.split()
    boundaries = []
    size = 0
    for _, value in parts[:-1]:
        size += len(value.split())
        boundaries.append(size)
    segments = SequenceMatcher(None, old, new, autojunk=False).get_opcodes()

    def new_boundary(index):
        for _, start, end, left, right in segments:
            if start <= index <= end and end > start:
                return left + round((index - start) * (right - left) / (end - start))
        return len(new)

    cuts = [0, *map(new_boundary, boundaries), len(new)]
    # cuts на один элемент длиннее пар: strict=False здесь и ниже намеренно.
    if any(left == right for left, right in zip(cuts, cuts[1:], strict=False)):
        raise DocxPatchError("Новую формулировку нельзя распределить по исходным абзацам")
    for (paragraph, original), left, right in zip(parts, cuts, cuts[1:], strict=False):
        leading = original[:len(original) - len(original.lstrip())]
        trailing = original[len(original.rstrip()):]
        replaced = _replace_in_paragraph(
            paragraph, original, leading + " ".join(new[left:right]) + trailing
        )
        if replaced != 1:
            return 0
    return 1


def _apply_text_patches(root: ET.Element, patches: Sequence[DocxPatch]) -> None:
    for patch in patches:
        matches = _text_matches(root, patch.source_ref, patch.old_text, patch.normalize_whitespace)
        if len(matches) != 1:
            context = patch.source_ref.get("section") or patch.source_ref.get("table") or "документ"
            detail = "не найден" if not matches else "найдено неоднозначно"
            raise DocxPatchError(f"Текст {patch.old_text!r} {detail} (источник: {context})")
        changed = _replace_text_match(matches[0], patch.new_text)
        if changed != 1:
            raise DocxPatchError(f"Не удалось заменить текст {patch.old_text!r}")


def patch_docx_bytes(document: bytes, patches: Sequence[DocxPatch]) -> bytes:
    """Return a DOCX copy with source-ref-addressed text changes applied.

    Only ``word/document.xml`` is rewritten; all other package parts retain
    their uncompressed contents. The input byte string is never modified.
    Ambiguous, stale, or unsupported locations fail before any result is
    returned.
    """

    _validate_docx(document)
    if not patches:
        return document
    for patch in patches:
        if not patch.old_text:
            raise DocxPatchError("old_text не может быть пустым")
        if any(character in patch.new_text for character in "\r\n\t"):
            raise DocxPatchError(
                "new_text должен быть одним текстовым фрагментом без табов и переносов"
            )
    active_patches = [patch for patch in patches if patch.old_text != patch.new_text]
    if not active_patches:
        return document
    return _rewrite_document_xml(document, lambda root: _apply_text_patches(root, active_patches))


@dataclass(frozen=True, slots=True)
class RowRemoval:
    """Delete one table row selected by parser ``source_ref`` coordinates.

    ``expect_code`` is the code one of the row cells must start with. A row that
    does not match is kept and reported: nothing is deleted by position alone.
    """

    source_ref: Mapping[str, object]
    expect_code: str


_NUMBERED_CELL = re.compile(r"^(\d+)([.)]?)$")


def _cell_texts(row: ET.Element) -> list[str]:
    return [
        "".join(node.text or "" for node in cell.iter(f"{W}t")).lstrip()
        for cell in row.findall(f"{W}tc")
    ]


def _starts_with_code(text: str, code: str) -> bool:
    """True when ``text`` begins with the whole ``code``, not a longer one."""
    return text.startswith(code) and (
        len(text) == len(code) or not text[len(code)].isalnum()
    )


def _renumber_rows(table: ET.Element) -> None:
    """Make a pure numbering column gap-free again after rows were deleted.

    Applies only when every data row has a single numeric text in its first
    cell; any other layout is left untouched.
    """

    data = []
    for row in table.findall(f"{W}tr")[1:]:
        cells = row.findall(f"{W}tc")
        texts = list(cells[0].iter(f"{W}t")) if cells else []
        if len(texts) != 1:
            return
        data.append((texts[0], _NUMBERED_CELL.fullmatch(texts[0].text or "")))
    if not data or not all(match for _, match in data):
        return
    if [int(match.group(1)) for _, match in data] == list(range(1, len(data) + 1)):
        return
    for position, (node, match) in enumerate(data, 1):
        _set_text(node, f"{position}{match.group(2)}")


def remove_table_rows(
    document: bytes, removals: Sequence[RowRemoval]
) -> tuple[bytes, tuple[str, ...]]:
    """Return a DOCX copy without the listed table rows, plus the rows kept.

    Coordinates are one-based ``table``/``row`` in the document *before* any
    removal, so every target is resolved against the original layout before the
    first row is deleted. A row is deleted only when one of its cells starts
    with ``expect_code`` and the row does not open a vertically merged cell;
    every other target stays in place and is described in the second value.
    A numbering column, when the table has one, is renumbered without gaps.
    """

    _validate_docx(document)
    if not removals:
        return document, ()

    def select(root: ET.Element) -> tuple[list[tuple[ET.Element, ET.Element]], list[str]]:
        body = root.find(f"{W}body")
        if body is None:
            raise DocxPatchError("В DOCX не найдено тело документа")
        tables = body.findall(f"{W}tbl")
        doomed: list[tuple[ET.Element, ET.Element]] = []
        kept: list[str] = []
        for removal in removals:
            table_number = _positive_int(removal.source_ref.get("table"), "table")
            row_number = _positive_int(removal.source_ref.get("row"), "row")
            where = f"таблица {table_number}, строка {row_number}"
            if table_number > len(tables) or row_number > len(
                tables[table_number - 1].findall(f"{W}tr")
            ):
                kept.append(f"{where}: такой строки нет в документе")
                continue
            table = tables[table_number - 1]
            row = table.findall(f"{W}tr")[row_number - 1]
            cells = _cell_texts(row)
            if not removal.expect_code or not any(
                _starts_with_code(text, removal.expect_code) for text in cells
            ):
                shown = " | ".join(cells)[:60]
                kept.append(
                    f"{where}: нет ячейки, начинающейся с «{removal.expect_code}» "
                    f"(строка: «{shown}»)"
                )
                continue
            if any(v.get(f"{W}val") == "restart" for v in row.iter(f"{W}vMerge")):
                kept.append(f"{where}: строка начинает объединённую по вертикали ячейку")
                continue
            if (table, row) not in doomed:
                doomed.append((table, row))
        return doomed, kept

    doomed, kept = select(_document_root(document))
    if not doomed:
        # Нечего удалять: файл возвращается байт в байт.
        return document, tuple(kept)

    def transform(root: ET.Element) -> None:
        touched = []
        for table, row in select(root)[0]:
            table.remove(row)
            if table not in touched:
                touched.append(table)
        for table in touched:
            _renumber_rows(table)

    return _rewrite_document_xml(document, transform), tuple(kept)


def _document_root(document: bytes) -> ET.Element:
    _validate_docx(document)
    source = ZipFile(BytesIO(document), "r")
    try:
        xml = source.read(DOCUMENT_PART)
    finally:
        source.close()
    try:
        return ET.fromstring(xml)
    except ET.ParseError as exc:
        raise DocxPatchError("Не удалось прочитать word/document.xml") from exc


def _row_cells(document: bytes, table: object, row: object) -> list[ET.Element]:
    root = _document_root(document)
    body = root.find(f"{W}body")
    if body is None:
        raise DocxPatchError("В DOCX не найдено тело документа")
    tables = body.findall(f"{W}tbl")
    table_number = _positive_int(table, "table")
    if table_number > len(tables):
        raise DocxPatchError(
            f"Таблица #{table_number} из source_ref не найдена; в документе {len(tables)} таблиц"
        )
    rows = tables[table_number - 1].findall(f"{W}tr")
    row_number = _positive_int(row, "row")
    if row_number > len(rows):
        raise DocxPatchError(
            f"Строка #{row_number} из source_ref не найдена в таблице #{table_number}"
        )
    return rows[row_number - 1].findall(f"{W}tc")


def read_row_cells(document: bytes, table: object, row: object) -> tuple[str, ...]:
    """Return the text of each physical cell of one table row."""
    return tuple(
        "\n".join(
            "".join(node.text or "" for node in paragraph.iter(f"{W}t"))
            for paragraph in cell.findall(f"{W}p")
        )
        for cell in _row_cells(document, table, row)
    )


def cell_has_vertical_merge(document: bytes, table: int, row: int, column: int) -> bool:
    cells = _row_cells(document, table, row)
    number = _positive_int(column, "col")
    if number > len(cells):
        raise DocxPatchError(f"В строке {row} нет столбца {column}")
    return cells[number - 1].find(f"{W}tcPr/{W}vMerge") is not None


def read_area_text(document: bytes, source_ref: Mapping[str, object]) -> tuple[str, ...]:
    """Return the visible text of each paragraph selected by ``source_ref``.

    Coordinates follow the same rules as :class:`DocxPatch`: ``table``/``row``/
    ``col`` select table cells, ``paragraph_index`` selects one body paragraph,
    an empty reference selects the whole body. Text is combined across runs in
    document order; runs and paragraphs without ``w:t`` contribute nothing.
    """

    return tuple(
        "".join(node.text or "" for node in paragraph.iter(f"{W}t"))
        for paragraph in _resolve_paragraphs(_document_root(document), source_ref)
    )
