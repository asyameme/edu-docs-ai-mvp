"""Apply precise text changes to an existing DOCX while retaining its layout.

The generator starts from the original document and edits only a uniquely
identified text value. This follows the project decision to replace changed
entities in-place and leave the rest of each source document untouched.
"""

from __future__ import annotations

from dataclasses import dataclass
from io import BytesIO
from typing import Mapping, Sequence
from xml.sax.saxutils import quoteattr
from zipfile import BadZipFile, ZIP_DEFLATED, ZipFile
import xml.etree.ElementTree as ET
import re


W_NS = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
W = f"{{{W_NS}}}"
DOCUMENT_PART = "word/document.xml"
_XML_SPACE = "{http://www.w3.org/XML/1998/namespace}space"


class DocxPatchError(ValueError):
    """Raised when a DOCX cannot be changed safely and unambiguously."""


@dataclass(frozen=True, slots=True)
class DocxPatch:
    """Replace one old text value at its parser ``source_ref`` location.

    ``table``, ``row``, ``col`` and ``paragraph_index`` in ``source_ref`` are
    one-based. If the column is omitted, the old text must identify exactly
    one paragraph in the selected row. If no table is supplied, the old text
    must occur exactly once in the document body. ``section`` is retained as
    diagnostic context; this patcher currently does not use
    ``paragraph_index`` to select a target.
    """

    source_ref: Mapping[str, object]
    old_text: str
    new_text: str


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
        return list(body.iter(f"{W}p"))

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


def _run_properties(run: ET.Element | None) -> bytes | None:
    if run is None:
        return None
    properties = run.find(f"{W}rPr")
    return ET.tostring(properties, encoding="utf-8") if properties is not None else b""


def _set_text(node: ET.Element, value: str) -> None:
    node.text = value
    if value[:1].isspace() or value[-1:].isspace():
        node.set(_XML_SPACE, "preserve")
    else:
        node.attrib.pop(_XML_SPACE, None)


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
    properties = {_run_properties(run) for run in involved_runs}
    if len(properties) > 1:
        raise DocxPatchError(
            "Искомый текст пересекает фрагменты с разным форматированием; "
            "автоматическая замена может изменить оформление"
        )
    if any(run is None for run in involved_runs):
        raise DocxPatchError("Не удалось безопасно определить форматирование текста")

    left, _ = spans[first]
    _set_text(text_nodes[first], values[first][: start - left] + new_text)
    for index in range(first + 1, last):
        _set_text(text_nodes[index], "")
    _, right = spans[last]
    _set_text(text_nodes[last], values[last][end - (right - len(values[last])) :])
    return 1


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
            raise DocxPatchError("new_text должен быть одним текстовым фрагментом без табов и переносов")
    active_patches = [patch for patch in patches if patch.old_text != patch.new_text]
    if not active_patches:
        return document

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

        for patch in active_patches:
            paragraphs = _resolve_paragraphs(root, patch.source_ref)
            matches: list[ET.Element] = []
            for paragraph in paragraphs:
                count = "".join(node.text or "" for node in paragraph.iter(f"{W}t")).count(patch.old_text)
                matches.extend([paragraph] * count)
            if len(matches) != 1:
                context = patch.source_ref.get("section") or patch.source_ref.get("table") or "документ"
                detail = "не найден" if not matches else "найден неоднозначно"
                raise DocxPatchError(f"Текст {patch.old_text!r} {detail} (источник: {context})")
            changed = _replace_in_paragraph(matches[0], patch.old_text, patch.new_text)
            if changed != 1:
                raise DocxPatchError(f"Не удалось заменить текст {patch.old_text!r}")

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
                payload = updated_xml if info.filename == DOCUMENT_PART else source.read(info.filename)
                target.writestr(info, payload)
        return output.getvalue()
    finally:
        source.close()
