"""Единый процесс сохраняет версии без переключателя и ручного подтверждения."""

from fastapi import FastAPI
from test_automatic_versions import automatic_snapshot, checked_job, read_generation_snapshot

from app.config import Settings
from app.domain import SnapshotV2
from app.review_api import router


def test_version_without_policy_marker_uses_automatic_process():
    _, _, data = automatic_snapshot()
    del data["review"]["lifecycle_policy"]
    assert SnapshotV2.model_validate(data).review.lifecycle_policy == "automatic_source_v2"


def test_process_has_no_feature_switch_or_confirmation_endpoint():
    assert "automatic_versions_enabled" not in Settings.model_fields
    app = FastAPI()
    app.include_router(router)
    assert not any(path.endswith("/confirm") for path in app.openapi()["paths"])


def test_default_process_exports_checked_warning_without_confirmation(monkeypatch):
    program_id, version_id, data = automatic_snapshot()
    del data["review"]["lifecycle_policy"]
    checked = checked_job(program_id, version_id, data)
    snapshot = read_generation_snapshot(monkeypatch, data, checked, program_id, version_id)
    assert snapshot.review.confirmation is None
    assert checked["report"]["result"]["issues"][0]["severity"] == "warning"
