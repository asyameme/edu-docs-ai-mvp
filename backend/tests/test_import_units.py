"""Границы пакета и сохранение наблюдений разных документов."""

import hashlib
import io
import threading
import zipfile
from types import SimpleNamespace
from uuid import uuid4

import pytest

from app.document_adapter import DocumentBuilder
from app.errors import DomainError
from app.import_archive import read_package
from app.parser_adapter import to_snapshot
from app.storage import OriginalStorage


def upload(members):
    stream = io.BytesIO()
    with zipfile.ZipFile(stream, "w") as archive:
        for name, content in members:
            info = zipfile.ZipInfo(name)
            info.filename = name
            archive.writestr(info, content)
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
    ],
)
def test_archive_rejects_empty_and_unsupported_documents(members):
    with pytest.raises(DomainError):
        read_package(upload(members), 1024 * 1024)


def test_archive_preserves_same_content_under_distinct_names():
    _, files = read_package(
        upload([("first.docx", b"same"), ("second.docx", b"same")]), 1024 * 1024
    )

    assert [item.name for item in files] == ["first.docx", "second.docx"]
    assert files[0].sha256 == files[1].sha256 == hashlib.sha256(b"same").hexdigest()


def test_archive_rejects_duplicate_member_names():
    with pytest.raises(DomainError, match="Повторяющееся имя"):
        read_package(upload([("one.docx", b"first"), ("ONE.DOCX", b"second")]), 1024 * 1024)


def test_storage_can_prepare_already_hashed_archive_bytes():
    content = b"archive member"
    digest = hashlib.sha256(content).hexdigest()
    stored = OriginalStorage(1024).save_bytes(
        "nested/source.docx", content, precomputed_sha256=digest
    )

    assert stored["content"] == content
    assert stored["original_name"] == "source.docx"
    assert stored["sha256"] == digest


def test_package_identity_preserves_names_that_select_the_parser():
    first, files = read_package(upload([("one.docx", b"one"), ("two.xlsx", b"two")]), 10000)
    reordered, _ = read_package(upload([("two.xlsx", b"two"), ("one.docx", b"one")]), 10000)
    renamed, _ = read_package(
        upload([("renamed.xlsx", b"two"), ("nested/new.docx", b"one")]), 10000
    )
    other_folder, _ = read_package(upload([("РПД/one.docx", b"one"), ("two.xlsx", b"two")]), 10000)
    assert first == reordered
    assert first != renamed
    assert first != other_folder
    assert [f.name for f in files] == ["one.docx", "two.xlsx"]


def test_archive_members_are_extracted_concurrently_and_keep_archive_order(monkeypatch):
    original_read = zipfile.ZipFile.read
    barrier = threading.Barrier(2)

    def concurrent_read(archive, member, pwd=None):
        if isinstance(member, zipfile.ZipInfo) and member.filename.endswith(".docx"):
            barrier.wait(timeout=2)
        return original_read(archive, member, pwd)

    monkeypatch.setattr(zipfile.ZipFile, "read", concurrent_read)
    names = ["first.docx", "second.docx", "third.docx", "fourth.docx"]
    _, files = read_package(upload([(name, name.encode()) for name in names]), 10000)

    assert [item.name for item in files] == names
    assert [item.content for item in files] == [name.encode() for name in names]


def test_pm_archive_preserves_matrix_without_sending_it_to_parser():
    documents = [
        ("РПД/Первая.docx", b"rpd-1"),
        ("РПД/Вторая.docx", b"rpd-2"),
        ("РПП/Практика.docx", b"rpp"),
        ("Учебный план/План.docx", b"plan"),
        ("Характеристика/Характеристика.docx", b"characteristic"),
        ("ЦКМВ/ЦКМВ.docx", b"ckmv"),
    ]
    matrix = ("Матрица компетенций/Матрица компетенций.xlsx", b"matrix")
    with_matrix, files = read_package(upload([matrix, *documents]), 10000)
    without_matrix, _ = read_package(upload(documents), 10000)
    assert with_matrix != without_matrix
    assert files[0].name == matrix[0]
    assert files[0].parse is False
    assert [(item.name, item.parse) for item in files[1:]] == [
        (name, True) for name, _ in documents
    ]


def test_archive_checks_compressed_size_and_member_count():
    with pytest.raises(DomainError) as failure:
        read_package(upload([("one.docx", b"one")]), 10)
    assert failure.value.status == 413
    with pytest.raises(DomainError):
        read_package(upload([(f"{i}.docx", str(i).encode()) for i in range(101)]), 100000)

    _, files = read_package(
        upload([
            ("Матрица компетенций/Матрица компетенций.xlsx", b"matrix"),
            *((f"РПД/{i}.docx", str(i).encode()) for i in range(100)),
        ]),
        100000,
    )
    assert len(files) == 101
    assert sum(file.parse for file in files) == 100


def test_archive_with_only_matrix_has_no_documents_to_parse():
    with pytest.raises(DomainError, match="нет документов для разбора"):
        read_package(
            upload([("Матрица компетенций/Матрица компетенций.xlsx", b"matrix")]),
            10000,
        )


def test_empty_archive_has_no_documents_to_parse():
    with pytest.raises(DomainError, match="нет документов для разбора"):
        read_package(upload([]), 10000)


def test_archive_may_have_one_wrapper_folder():
    _, files = read_package(
        upload([
            ("данные/Матрица компетенций/Матрица компетенций.xlsx", b"matrix"),
            ("данные/РПД/РПД.docx", b"rpd"),
        ]),
        10000,
    )
    assert [file.parse for file in files] == [False, True]


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
            "meta": {
                "schema_version": 2,
                "doc_type": "RPD",
                "file_name": "source.docx",
                "file_hash": "a" * 64,
                "parser_config": "artificial-unit-fixture",
                "program_id": "26/5003/1",
            },
            "disciplines": [
                {
                    "name": "Одинаковое имя",
                    "semesters": [1],
                    "credit_units": 3,
                    "hours_total": hours,
                    "hours_by_kind": {"лекции": hours},
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


def test_adapter_uses_section_to_resolve_duplicate_profile_codes():
    document_id = str(uuid4())
    builder = DocumentBuilder(uuid4())
    base_section = "Профессиональные перекрестные компетенции базового уровня"
    top_section = "Профессиональные перекрестные компетенции топ-уровня"
    base_id = builder.entity(
        document_id,
        "competency",
        "ПКП-ППК-ДА2",
        {
            "code": "ПКП-ППК-ДА2",
            "source_ref": {
                "file": "цкмв.docx",
                "table": 1,
                "row": 105,
                "section": base_section,
            },
        },
    )
    builder.entity(
        document_id,
        "competency",
        "ПКП-ППК-ДА2",
        {
            "code": "ПКП-ППК-ДА2",
            "source_ref": {
                "file": "цкмв.docx",
                "table": 1,
                "row": 221,
                "section": top_section,
            },
        },
    )

    resolved = builder.resolve(
        document_id,
        "competency",
        "ПКП-ППК-ДА2",
        {
            "file": "цкмв.docx",
            "table": 1,
            "row": 106,
            "section": base_section,
        },
    )

    assert resolved == base_id
