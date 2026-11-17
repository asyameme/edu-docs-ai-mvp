"""Единое каноническое представление JSON и буквальные данные в ссылочных сообщениях."""

import pytest

from app.graph_storage import canonical_bytes, pack_tree, unpack_tree


def test_canonical_json_uses_utf8_and_ecmascript_numbers():
    assert canonical_bytes({"z": 1.0, "a": "Ё"}) == '{"a":"Ё","z":1}'.encode()
    assert canonical_bytes({"a": -0.0}) == b'{"a":0}'
    with pytest.raises(ValueError):
        canonical_bytes({"a": float("nan")})


def test_literal_rule_configuration_is_not_interpreted_as_graph_reference():
    value = {"ruleset": {"config": {"__graph_state": "untrusted", "review": None}}}
    assert pack_tree(None, None, value) == value
    assert unpack_tree(None, value) == value


def test_legacy_version_uses_current_dto_without_rewriting_original():
    from copy import deepcopy
    from uuid import uuid4

    from app.domain import SnapshotData, VersionRead
    from app.repository import version_dto

    original = {"schema_version": 1, **SnapshotData().model_dump(mode="json")}
    saved = deepcopy(original)
    value = VersionRead.model_validate(
        version_dto(
            {
                "id": uuid4(),
                "program_id": uuid4(),
                "number": 1,
                "parent_version_id": None,
                "restored_from_version_id": None,
                "origin": "initial",
                "created_at": "2026-11-12T00:00:00Z",
                "snapshot": original,
            }
        )
    )
    assert value.snapshot.schema_version == 2
    assert original == saved
