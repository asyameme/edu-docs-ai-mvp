"""Проверка локального подключения настоящего парсера к Backend."""

from hashlib import sha256
from io import BytesIO
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4
from zipfile import ZipFile

import pytest

from app.import_archive import read_package
from app.local_parser import parse_local_document
from app.parser_adapter import to_snapshot
from app.parser_document import parse_output

ROOT = Path(__file__).resolve().parents[2]


@pytest.mark.parametrize(
    ("name", "kind"),
    [
        ("РПД/РПД_матанализ1.docx", "RPD"),
        ("РПП/РПП.docx", "RPP"),
        ("Учебный план/UP.docx", "CURRICULUM"),
        ("Характеристика/Общая характеристика.docx", "CHARACTERISTIC"),
        ("ЦКМВ/ЦКМВ.docx", "CKMV"),
    ],
)
def test_real_docx_reaches_backend_graph(name, kind):
    original = (ROOT / "tests" / "originals" / Path(name).name).read_bytes()
    program_id, document_id = uuid4(), uuid4()
    output = parse_local_document(name, original, program_id)
    parsed = parse_output(output)
    assert parsed.meta.doc_type == kind
    assert parsed.meta.file_hash == sha256(original).hexdigest()
    snapshot, _ = to_snapshot(program_id, [{"document_id": document_id, "output": output}])
    assert snapshot["entities"]
    assert snapshot["source_refs"]
    if kind == "CHARACTERISTIC":
        assert any(entity["kind"] == "program" for entity in snapshot["entities"])
    else:
        assert snapshot["relations"]
    assert snapshot["metadata"]["parsed_documents"][str(document_id)] == output


def test_folder_selects_parser_instead_of_guessing_other_document_type():
    original = (ROOT / "tests" / "originals" / "РПД_матанализ1.docx").read_bytes()
    for name in ("РПП/документ.docx", "данные/РПП/документ.docx"):
        with pytest.raises(ValueError, match="Не удалось разобрать DOCX"):
            parse_local_document(name, original, uuid4())


def test_pm_package_builds_graph_from_all_documents_without_matrix():
    originals = ROOT / "tests" / "originals"
    members = [
        ("РПД/РПД_матанализ1.docx", "РПД_матанализ1.docx"),
        ("РПД/РПД_БД.docx", "РПД_БД.docx"),
        ("РПП/РПП.docx", "РПП.docx"),
        ("Учебный план/Учебный план.docx", "UP.docx"),
        ("Характеристика/Характеристика.docx", "Общая характеристика.docx"),
        ("ЦКМВ/ЦКМВ.docx", "ЦКМВ.docx"),
    ]
    archive_bytes = BytesIO()
    with ZipFile(archive_bytes, "w") as archive:
        for name, source in members:
            archive.writestr(name, (originals / source).read_bytes())
        archive.writestr(
            "Матрица компетенций/Матрица компетенций.xlsx",
            (originals / "Матрица компетенций.xlsx").read_bytes(),
        )
    archive_bytes.seek(0)
    _, files = read_package(
        SimpleNamespace(filename="данные.zip", file=archive_bytes), 500 * 1024**2
    )
    assert [file.name for file in files if file.parse] == [name for name, _ in members]
    assert len([file for file in files if not file.parse]) == 1

    program_id = uuid4()
    results = [
        {
            "document_id": uuid4(),
            "output": parse_local_document(file.name, file.content, program_id),
        }
        for file in files if file.parse
    ]
    assert [item["output"]["meta"]["doc_type"] for item in results] == [
        "RPD", "RPD", "RPP", "CURRICULUM", "CHARACTERISTIC", "CKMV",
    ]
    snapshot, _ = to_snapshot(program_id, results)
    assert len(snapshot["document_ids"]) == len(members)
    assert len(snapshot["metadata"]["parsed_documents"]) == len(members)
    assert snapshot["entities"] and snapshot["relations"] and snapshot["source_refs"]


def test_xlsx_has_explicit_unsupported_format_error():
    original = (ROOT / "tests" / "originals" / "UP_short.xlsx").read_bytes()
    with pytest.raises(ValueError, match="XLSX"):
        parse_local_document("UP_short.xlsx", original, uuid4())


def test_invalid_docx_is_reported_as_invalid_document():
    with pytest.raises(ValueError, match="Не удалось разобрать DOCX"):
        parse_local_document("broken.docx", b"not a docx", uuid4())
