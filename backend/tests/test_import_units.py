"""Границы пакета и сохранение наблюдений разных документов."""

import io
import zipfile
from types import SimpleNamespace
from uuid import uuid4

import pytest

from app.errors import DomainError
from app.import_archive import read_package
from app.parser_adapter import to_snapshot


def upload(members):
    stream = io.BytesIO()
    with zipfile.ZipFile(stream, "w") as archive:
        for name, content in members:
            archive.writestr(name, content)
    stream.seek(0)
    return SimpleNamespace(filename="package.zip", file=stream)


@pytest.mark.parametrize("name", ["../bad.docx", "/bad.docx", "C:/bad.docx", "a\\bad.docx"])
def test_archive_rejects_unsafe_paths(name):
    with pytest.raises(DomainError) as failure:
        read_package(upload([(name, b"x")]), 1024 * 1024)
    assert failure.value.status == 422


@pytest.mark.parametrize(
    "members",
    [
        [("empty.docx", b"")],
        [("bad.txt", b"x")],
        [("one.docx", b"same"), ("two.docx", b"same")],
    ],
)
def test_archive_rejects_empty_unsupported_and_duplicate_documents(members):
    with pytest.raises(DomainError):
        read_package(upload(members), 1024 * 1024)


def test_package_identity_is_independent_of_zip_order_and_names():
    first, files = read_package(upload([("one.docx", b"one"), ("two.xlsx", b"two")]), 10000)
    second, _ = read_package(upload([("renamed.xlsx", b"two"), ("nested/new.docx", b"one")]), 10000)
    assert first == second
    assert [f.name for f in files] == ["one.docx", "two.xlsx"]


def test_archive_checks_compressed_size_and_member_count():
    with pytest.raises(DomainError) as failure:
        read_package(upload([("one.docx", b"one")]), 10)
    assert failure.value.status == 413
    with pytest.raises(DomainError):
        read_package(upload([(f"{i}.docx", str(i).encode()) for i in range(51)]), 100000)


def test_archive_rejects_damaged_compressed_document():
    stream = io.BytesIO()
    with zipfile.ZipFile(stream, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("source.docx", b"document" * 50)
    damaged = bytearray(stream.getvalue())
    payload_offset = 30 + len("source.docx")
    damaged[payload_offset] = 7
    package = SimpleNamespace(filename="damaged.zip", file=io.BytesIO(damaged))
    with pytest.raises(DomainError) as failure:
        read_package(package, 10000)
    assert failure.value.status == 422


def observation(document_id, hours):
    return {
        "document_id": document_id,
        "output": {
            "disciplines": [
                {
                    "name": "Одинаковое имя",
                    "hours_total": hours,
                    "source_ref": {"file": "source.docx", "section": "2"},
                }
            ],
            "relations": [],
            "parse_issues": [],
        },
    }


def test_adapter_preserves_conflicting_observations_and_stable_ids():
    program, first, second = uuid4(), uuid4(), uuid4()
    files = [observation(first, 108), observation(second, 144)]
    snapshot, issues = to_snapshot(program, files)
    assert issues == []
    assert {e["attributes"]["hours_total"] for e in snapshot["entities"]} == {108, 144}
    assert len({e["id"] for e in snapshot["entities"]}) == 2
    assert snapshot == to_snapshot(program, files)[0]
    assert {r["document_id"] for r in snapshot["source_refs"]} == {str(first), str(second)}


def test_adapter_retains_unresolved_relation_instead_of_guessing():
    first = observation(uuid4(), 108)
    first["output"]["relations"] = [
        {
            "src_entity": "discipline",
            "src_key": "Одинаковое имя",
            "dst_entity": "competency",
            "dst_key": "UNKNOWN",
            "kind": "формирует",
            "source_ref": {"file": "source.docx"},
        }
    ]
    snapshot, issues = to_snapshot(uuid4(), [first])
    assert snapshot["relations"] == []
    assert snapshot["metadata"]["unresolved_relations"][0]["dst_key"] == "UNKNOWN"
    assert issues[0]["category"] == "unresolved_relation"
