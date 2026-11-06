"""Только V2 на границах приложения без поддержки прежнего формата."""

from uuid import uuid4

import pytest
from pydantic import ValidationError

from app.domain import SnapshotV2, VersionCreate, VersionRead
from app.review_contract import PrepareRequest


def test_version_save_accepts_v2_and_rejects_v1():
    result = VersionCreate(expected_version_id=uuid4(), snapshot={"schema_version": 2})
    assert isinstance(result.snapshot, SnapshotV2)
    with pytest.raises(ValidationError):
        VersionCreate(expected_version_id=uuid4(), snapshot={"schema_version": 1})


def test_public_snapshot_schemas_have_only_v2():
    for model in (VersionCreate, VersionRead, PrepareRequest):
        definitions = model.model_json_schema()["$defs"]
        assert "Snapshot" not in definitions
        assert definitions["SnapshotV2"]["properties"]["schema_version"]["const"] == 2
