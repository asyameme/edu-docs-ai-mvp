"""Проверка локального подключения настоящего парсера к Backend."""

from hashlib import sha256
from pathlib import Path
from uuid import uuid4

import pytest

from app.local_parser import parse_local_document
from app.parser_adapter import to_snapshot
from app.parser_document import parse_output

ROOT = Path(__file__).resolve().parents[2]


@pytest.mark.parametrize(
    ("name", "kind"),
    [
        ("РПД_матанализ1.docx", "RPD"),
        ("РПП.docx", "RPP"),
        ("UP.docx", "CURRICULUM"),
    ],
)
def test_real_docx_reaches_backend_graph(name, kind):
    original = (ROOT / "tests" / "originals" / name).read_bytes()
    program_id, document_id = uuid4(), uuid4()
    output = parse_local_document(name, original, program_id)
    parsed = parse_output(output)
    assert parsed.meta.doc_type == kind
    assert parsed.meta.file_hash == sha256(original).hexdigest()
    snapshot, _ = to_snapshot(program_id, [{"document_id": document_id, "output": output}])
    assert snapshot["entities"]
    assert snapshot["relations"]
    assert snapshot["metadata"]["parsed_documents"][str(document_id)] == output


def test_xlsx_has_explicit_unsupported_format_error():
    original = (ROOT / "tests" / "originals" / "UP_short.xlsx").read_bytes()
    with pytest.raises(ValueError, match="XLSX"):
        parse_local_document("UP_short.xlsx", original, uuid4())


def test_invalid_docx_is_reported_as_invalid_document():
    with pytest.raises(ValueError, match="Не удалось разобрать DOCX"):
        parse_local_document("broken.docx", b"not a docx", uuid4())
