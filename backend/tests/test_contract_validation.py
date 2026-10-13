"""Контракты отклоняют недопустимые для PostgreSQL строки до обращения к базе."""

from uuid import uuid4

import pytest
from pydantic import ValidationError

from app.domain import ProgramCreate, Snapshot


def test_program_name_rejects_nul():
    with pytest.raises(ValidationError, match="NUL"):
        ProgramCreate(name="bad\x00name")


@pytest.mark.parametrize(
    "state",
    [
        {"metadata": {"note": "bad\x00value"}},
        {"metadata": {"bad\x00key": "value"}},
        {"metadata": {"nested": [{"note": "bad\x00value"}]}},
        {"entities": [{"id": str(uuid4()), "kind": "discipline",
                       "attributes": {"name": "bad\x00name"}}]},
    ],
)
def test_snapshot_rejects_nul_in_json_strings(state):
    with pytest.raises(ValidationError, match="NUL"):
        Snapshot.model_validate(state)


def test_valid_unicode_null_values_and_literal_escape_are_preserved():
    state = {"metadata": {"note": "Русский текст 😀", "unknown": None,
                          "literal": r"\u0000", "nested": ["line\nbreak", 0]}}
    assert Snapshot.model_validate(state).model_dump(mode="json")["metadata"] == state["metadata"]
    assert ProgramCreate(name=" Русское название 😀 ").name == "Русское название 😀"
