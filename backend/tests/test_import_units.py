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
        [("one.docx", b"same"), ("two.docx", b"same")],
    ],
)
def test_archive_rejects_empty_unsupported_and_duplicate_documents(members):
    with pytest.raises(DomainError):
        read_package(upload(members), 1024 * 1024)


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
