"""Пограничные случаи выбора JSON-значений и неизменяемых Markdown-файлов."""

import hashlib
import os
import stat
import threading
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace
from uuid import uuid4

import pytest

from app.errors import DomainError
from app.review_activities import ReviewActivities
from app.review_logic import apply_changes, digest
from tests.test_review_logic import change, sample


@pytest.mark.parametrize(
    "first,second", [(0, False), (1, True), ({"x": 0}, {"x": False}), ([0], [False])]
)
def test_choices_with_different_json_types_are_conflicting(first, second):
    state = sample()
    left, right = change(state, first), change(state, second)
    left["field_path"] = right["field_path"] = "/attributes/selection"
    with pytest.raises(DomainError, match="противоречат"):
        apply_changes(state, [left, right])


@pytest.mark.parametrize("same_content", [True, False])
def test_concurrent_artifact_publication_is_immutable_and_idempotent(
    tmp_path, monkeypatch, same_content
):
    activities = object.__new__(ReviewActivities)
    activities.settings = SimpleNamespace(
        artifact_files_path=tmp_path, analysis_max_response_bytes=1024
    )
    identity = str(uuid4())
    barrier = threading.Barrier(2)
    real_fsync = os.fsync

    def synchronized_fsync(fd):
        real_fsync(fd)
        if stat.S_ISREG(os.fstat(fd).st_mode):
            barrier.wait(timeout=5)

    monkeypatch.setattr(os, "fsync", synchronized_fsync)

    def publish(text):
        artifact = {
            "id": identity,
            "document_id": str(uuid4()),
            "parser_revision": "test",
            "parser_config": {},
            "parser_config_hash": digest({}),
            "markdown": text,
        }
        try:
            stored = activities.save_artifacts({"artifacts": [artifact]})[0]
            return stored
        except Exception as error:
            return error

    values = ["# Первый Markdown", "# Первый Markdown" if same_content else "# Другой Markdown"]
    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(publish, values))
    successes = [result for result in results if isinstance(result, dict)]
    failures = [result for result in results if isinstance(result, Exception)]
    if same_content:
        assert len(successes) == 2 and not failures
    else:
        assert len(successes) == 1 and len(failures) == 1
        assert isinstance(failures[0], ValueError)
    final_hash = hashlib.sha256((tmp_path / identity).read_bytes()).hexdigest()
    assert all(result["sha256"] == final_hash for result in successes)
    assert not list(tmp_path.glob(".upload-*"))
