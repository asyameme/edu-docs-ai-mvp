"""Чтение фрагментов обычных исходных документов для инструментов чата."""

import asyncio
import json
import os
from pathlib import Path
from types import SimpleNamespace
from uuid import UUID, uuid4

import httpx
import pytest
from psycopg_pool import ConnectionPool
from pydantic_ai import ModelRetry
from test_generation_docx_patch import _paragraph, _table, make_docx
from test_review_integration import choose, prepare

import worker.agent.read_adapter as read_adapter
from app.config import get_settings
from app.domain import SnapshotV2, SourceRefV2
from worker.agent.read_adapter import PostgresChatReadPort


@pytest.mark.parametrize(
    ("body", "coordinates", "expected", "full_artifact"),
    [
        (
            _paragraph("Сохранённый фрагмент"),
            {"paragraph_index": 1},
            "Сохранённый фрагмент",
            False,
        ),
        (
            _table("Ячейка таблицы", "Соседняя ячейка"),
            {"table": 1, "row": 1, "col": 1},
            "Ячейка таблицы",
            True,
        ),
    ],
)
def test_reads_original_docx_fragment_by_parser_coordinates(
    monkeypatch, tmp_path, body, coordinates, expected, full_artifact
):
    program_id, document_id, version_id, source_id = uuid4(), uuid4(), uuid4(), uuid4()
    source = SourceRefV2(
        id=source_id,
        document_id=document_id,
        locator=json.dumps(coordinates),
        artifact_id=uuid4() if full_artifact else None,
        artifact_locator="document" if full_artifact else None,
    )
    original = make_docx(body)
    port = PostgresChatReadPort(object())
    port.snapshot = lambda *_: SnapshotV2(document_ids=[document_id], source_refs=[source])
    monkeypatch.setattr(
        read_adapter.Repository,
        "document_content",
        lambda _repo, _program, _document: {
            "content": original,
            "original_name": "источник.docx",
        },
    )
    monkeypatch.setattr(
        read_adapter,
        "get_settings",
        lambda: SimpleNamespace(artifact_files_path=Path(tmp_path)),
    )

    fragment = asyncio.run(
        port.read_source_fragment(
            program_id=program_id,
            version_id=version_id,
            source_ref_id=source_id,
            context_paragraphs=0,
        )
    )

    assert fragment.document_name == "источник.docx"
    assert fragment.text == expected


@pytest.mark.parametrize(
    "coordinates",
    [
        {},
        {"paragraph_index": 0},
        {"paragraph_index": 10},
        {"table": 1, "col": 1},
    ],
)
def test_rejects_missing_or_invalid_docx_coordinates(monkeypatch, coordinates):
    program_id, document_id, version_id, source_id = (uuid4() for _ in range(4))
    source = SourceRefV2(
        id=source_id,
        document_id=document_id,
        locator=json.dumps(coordinates),
    )
    port = PostgresChatReadPort(object())
    port.snapshot = lambda *_: SnapshotV2(document_ids=[document_id], source_refs=[source])
    monkeypatch.setattr(
        read_adapter.Repository,
        "document_content",
        lambda *_: {
            "content": make_docx(_paragraph("Исходный текст")),
            "original_name": "источник.docx",
        },
    )

    with pytest.raises(ModelRetry, match="Не удалось выполнить чтение"):
        asyncio.run(
            port.read_source_fragment(
                program_id=program_id,
                version_id=version_id,
                source_ref_id=source_id,
                context_paragraphs=0,
            )
        )

def test_rejects_source_reference_outside_selected_version():
    document_id, version_id = uuid4(), uuid4()
    port = PostgresChatReadPort(object())
    port.snapshot = lambda *_: SnapshotV2(document_ids=[document_id])

    with pytest.raises(ModelRetry, match="Фрагмент источника недоступен"):
        asyncio.run(
            port.read_source_fragment(
                program_id=uuid4(),
                version_id=version_id,
                source_ref_id=uuid4(),
                context_paragraphs=0,
            )
        )

def test_docx_fragment_is_bounded(monkeypatch):
    program_id, document_id, version_id, source_id = (uuid4() for _ in range(4))
    source = SourceRefV2(
        id=source_id,
        document_id=document_id,
        locator=json.dumps({"paragraph_index": 1}),
    )
    port = PostgresChatReadPort(object())
    port.snapshot = lambda *_: SnapshotV2(document_ids=[document_id], source_refs=[source])
    monkeypatch.setattr(
        read_adapter.Repository,
        "document_content",
        lambda *_: {
            "content": make_docx(_paragraph("Т" * 30000)),
            "original_name": "источник.docx",
        },
    )

    fragment = asyncio.run(
        port.read_source_fragment(
            program_id=program_id,
            version_id=version_id,
            source_ref_id=source_id,
            context_paragraphs=2,
        )
    )

    assert len(fragment.text) <= 6000
    assert len(fragment.surrounding_text) <= 12000


def test_markdown_line_locator_remains_supported(monkeypatch, tmp_path):
    program_id, document_id, version_id, source_id, artifact_id = (uuid4() for _ in range(5))
    artifact_key = uuid4()
    (tmp_path / str(artifact_key)).write_text(
        "Первая строка\nЦитата\nТретья строка", encoding="utf-8"
    )
    source = SourceRefV2(
        id=source_id,
        document_id=document_id,
        locator="original locator",
        artifact_id=artifact_id,
        artifact_locator="line:2",
    )

    class Cursor:
        def __init__(self):
            self.row = None

        def __enter__(self):
            return self

        def __exit__(self, *_):
            return None

        def execute(self, query, _params):
            self.row = (str(artifact_key),) if "storage_key" in query else ("источник.docx",)
            return self

        def fetchone(self):
            return self.row

    class Pool:
        def connection(self):
            return Cursor()

    monkeypatch.setattr(
        read_adapter,
        "get_settings",
        lambda: SimpleNamespace(artifact_files_path=Path(tmp_path)),
    )
    port = PostgresChatReadPort(Pool())
    port.snapshot = lambda *_: SnapshotV2(document_ids=[document_id], source_refs=[source])

    fragment = asyncio.run(
        port.read_source_fragment(
            program_id=program_id,
            version_id=version_id,
            source_ref_id=source_id,
            context_paragraphs=1,
        )
    )

    assert fragment.text == "Цитата"
    assert fragment.surrounding_text == "Первая строка\nЦитата\nТретья строка"


@pytest.mark.integration
@pytest.mark.skipif(
    os.getenv("REVIEW_FIXTURE_TESTS") != "1",
    reason="Требуются отдельные тестовые HTTP, PostgreSQL и Temporal сервисы",
)
def test_reads_docx_source_after_import_and_review_from_postgres():
    original_text = "Исходная цитата из документа"
    original = make_docx(_paragraph(original_text))
    settings = get_settings()
    with httpx.Client(base_url="http://frontend", timeout=30, trust_env=False) as http:
        root, _, review = prepare(
            http,
            "РПД/РПД_матанализ1.docx",
            content=original,
        )
        version, _ = choose(http, root, review)
        source = next(
            item
            for item in version["snapshot"]["source_refs"]
            if item["artifact_locator"] == "document"
        )
        pool = ConnectionPool(
            kwargs={
                "host": settings.postgres_host,
                "dbname": settings.postgres_db,
                "user": settings.postgres_user,
                "password": settings.postgres_password.get_secret_value(),
            }
        )
        try:
            port = PostgresChatReadPort(pool)
            stored_snapshot = port.snapshot(UUID(version["program_id"]), UUID(version["id"]))
            assert UUID(source["id"]) in {item.id for item in stored_snapshot.source_refs}, {
                "response": source,
                "stored": [item.model_dump(mode="json") for item in stored_snapshot.source_refs],
            }
            fragment = asyncio.run(
                port.read_source_fragment(
                    program_id=UUID(version["program_id"]),
                    version_id=UUID(version["id"]),
                    source_ref_id=UUID(source["id"]),
                    context_paragraphs=0,
                )
            )
            with pytest.raises(ModelRetry, match="Фрагмент источника недоступен"):
                asyncio.run(
                    port.read_source_fragment(
                        program_id=uuid4(),
                        version_id=UUID(version["id"]),
                        source_ref_id=UUID(source["id"]),
                        context_paragraphs=0,
                    )
                )
            with pytest.raises(ModelRetry, match="Фрагмент источника недоступен"):
                asyncio.run(
                    port.read_source_fragment(
                        program_id=UUID(version["program_id"]),
                        version_id=UUID(version["id"]),
                        source_ref_id=uuid4(),
                        context_paragraphs=0,
                    )
                )
        finally:
            pool.close()

    assert fragment.document_name == "РПД_матанализ1.docx"
    assert fragment.text == original_text
