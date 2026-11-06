"""Проверка передачи настоящего DOCX через Activity Worker без HTTP-парсера."""

from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest

from app import import_activities
from app.parser_adapter import to_snapshot

ROOT = Path(__file__).resolve().parents[2]


@pytest.mark.parametrize(
    ("name", "expected_type"),
    [("РПД_матанализ1.docx", "RPD"), ("РПП.docx", "RPP"), ("UP.docx", "CURRICULUM")],
)
def test_worker_activity_stores_real_parser_output_without_url(monkeypatch, name, expected_type):
    program_id, document_id, job_id = uuid4(), uuid4(), uuid4()
    content = (ROOT / "tests" / "originals" / name).read_bytes()
    from hashlib import sha256

    saved = []
    job = {
        "program_id": program_id,
        "attempt": 1,
        "files": [{
            "document_id": document_id,
            "member_name": name,
            "sha256": sha256(content).hexdigest(),
            "status": "pending",
        }],
    }

    class Repo:
        def __init__(self, pool):
            self.pool = pool

        def job(self, requested_id, include_output=False):
            assert requested_id == job_id and include_output
            return job

        def file_finished(self, *args, **kwargs):
            saved.append((args, kwargs))

    class Originals:
        def __init__(self, pool):
            pass

        def document_content(self, requested_program, requested_document):
            assert requested_program == program_id
            assert requested_document == str(document_id)
            return {"content": content}

    monkeypatch.setattr(import_activities, "ImportRepository", Repo)
    monkeypatch.setattr(import_activities, "Repository", Originals)
    activity = import_activities.ImportActivities(object(), SimpleNamespace(parser_url=""))
    activity.parse({"job_id": str(job_id), "document_id": str(document_id), "attempt": 1})
    assert len(saved) == 1
    assert saved[0][0] == (job_id, document_id, 1)
    output = saved[0][1]["output"]
    assert output["meta"]["doc_type"] == expected_type
    snapshot, _ = to_snapshot(program_id, [{"document_id": document_id, "output": output}])
    assert snapshot["entities"] and snapshot["relations"]


def test_worker_marks_unsupported_xlsx_without_url(monkeypatch):
    program_id, document_id, job_id = uuid4(), uuid4(), uuid4()
    content = (ROOT / "tests" / "originals" / "UP_short.xlsx").read_bytes()
    saved = []
    job = {
        "program_id": program_id,
        "attempt": 1,
        "files": [
            {"document_id": document_id, "member_name": "UP_short.xlsx", "status": "pending"}
        ],
    }

    class Repo:
        def __init__(self, pool):
            self.pool = pool

        def job(self, *args, **kwargs):
            return job

        def file_finished(self, *args, **kwargs):
            saved.append(kwargs)

    class Originals:
        def __init__(self, pool):
            pass

        def document_content(self, *args):
            return {"content": content}

    monkeypatch.setattr(import_activities, "ImportRepository", Repo)
    monkeypatch.setattr(import_activities, "Repository", Originals)
    activity = import_activities.ImportActivities(object(), SimpleNamespace(parser_url=""))
    activity.parse({"job_id": str(job_id), "document_id": str(document_id), "attempt": 1})
    assert saved == [{
        "code": "parser_invalid_result",
        "message": "Документ не прочитан или ответ парсера не соответствует контракту",
    }]
