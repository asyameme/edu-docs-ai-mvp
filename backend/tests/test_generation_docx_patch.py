"""Тесты точечной замены DOCX: абзацный указатель и чтение текста по координатам."""

from io import BytesIO
from zipfile import ZIP_DEFLATED, ZipFile

import pytest

from worker.generation import DocxPatch, DocxPatchError, patch_docx_bytes, read_area_text

NS = 'xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"'
CONTENT_TYPES = (
    '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
    '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
    "<Default Extension=\"rels\" "
    'ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
    '<Default Extension="xml" ContentType="application/xml"/>'
    "<Override PartName=\"/word/document.xml\" "
    'ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/>'
    "</Types>"
)


def _paragraph(text: str) -> str:
    return f"<w:p><w:r><w:t>{text}</w:t></w:r></w:p>"


def _table(*cells: str) -> str:
    row = "".join(f"<w:tc>{_paragraph(text)}</w:tc>" for text in cells)
    return f"<w:tbl><w:tr>{row}</w:tr></w:tbl>"


def make_docx(*body_items: str, extra_parts: dict[str, str] | None = None) -> bytes:
    document = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        f"<w:document {NS}><w:body>{''.join(body_items)}</w:body></w:document>"
    )
    parts = {"[Content_Types].xml": CONTENT_TYPES, "word/document.xml": document}
    parts.update(extra_parts or {})
    buffer = BytesIO()
    with ZipFile(buffer, "w", compression=ZIP_DEFLATED) as archive:
        for name, payload in parts.items():
            archive.writestr(name, payload)
    return buffer.getvalue()


def document_text(docx: bytes) -> str:
    with ZipFile(BytesIO(docx)) as archive:
        return archive.read("word/document.xml").decode("utf-8")


def test_paragraph_index_selects_exact_occurrence():
    docx = make_docx(_paragraph("Показатель: 72 часа"), _paragraph("Показатель: 72 часа"))
    ambiguous_ref = {"section": "1"}
    with pytest.raises(DocxPatchError, match="неоднозначно"):
        patch_docx_bytes(
            docx, [DocxPatch(ambiguous_ref, "Показатель: 72 часа", "Показатель: 108 часа")]
        )
    patched = patch_docx_bytes(
        docx, [DocxPatch({"paragraph_index": 2}, "Показатель: 72 часа", "Показатель: 108 часа")]
    )
    text = document_text(patched)
    assert text.count("Показатель: 108 часа") == 1
    assert text.count("Показатель: 72 часа") == 1


def _patch(source_ref, old_text, new_text):
    return DocxPatch(source_ref=source_ref, old_text=old_text, new_text=new_text)


def test_paragraph_index_numbers_table_paragraphs():
    docx = make_docx(
        _paragraph("Первый"), _table("Второй", "Ячейка рядом"), _paragraph("Третий")
    )
    patched = patch_docx_bytes(docx, [_patch({"paragraph_index": 2}, "Второй", "Второй+")])
    assert "Второй+" in document_text(patched)


def test_paragraph_index_out_of_range():
    docx = make_docx(_paragraph("Один"))
    with pytest.raises(DocxPatchError, match="Абзац #5"):
        patch_docx_bytes(docx, [_patch({"paragraph_index": 5}, "Один", "Два")])


def test_row_and_col_require_table():
    docx = make_docx(_paragraph("Один"))
    with pytest.raises(DocxPatchError, match="требуют source_ref.table"):
        patch_docx_bytes(docx, [_patch({"row": 1}, "Один", "Два")])


def test_read_area_text_variants():
    docx = make_docx(
        _paragraph("Введение"),
        _table("Дисциплина", "72 ч"),
        _paragraph("Заключение"),
    )
    assert read_area_text(docx, {}) == ("Введение", "Дисциплина", "72 ч", "Заключение")
    assert read_area_text(docx, {"paragraph_index": 3}) == ("72 ч",)
    assert read_area_text(docx, {"table": 1, "row": 1, "col": 2}) == ("72 ч",)
    assert read_area_text(docx, {"table": 1, "row": 1}) == ("Дисциплина", "72 ч")
    with pytest.raises(DocxPatchError, match="Абзац #9"):
        read_area_text(docx, {"paragraph_index": 9})


def test_patch_keeps_other_package_parts():
    docx = make_docx(
        _paragraph("Заголовок"),
        extra_parts={"word/styles.xml": "<w:styles xmlns:w='urn:x'/>"},
    )
    patched = patch_docx_bytes(docx, [_patch({}, "Заголовок", "Заголовок+")])
    with ZipFile(BytesIO(docx)) as original, ZipFile(BytesIO(patched)) as result:
        assert result.read("word/styles.xml") == original.read("word/styles.xml")
        assert result.read("word/document.xml") != original.read("word/document.xml")
