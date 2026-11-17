"""Проверки выбора значений до сборки нормализованного графа."""

import pytest
from pydantic import ValidationError

from app.document_reconciliation import (
    apply_document_mutations,
    validate_document_mutation_variants,
    validate_document_mutations,
)
from app.errors import DomainError

DOC = "00000000-0000-0000-0000-000000000001"


def mutation(path, old, value, document=DOC):
    return {
        "op": "set_document_value",
        "document_id": document,
        "field_path": path,
        "expected_old_value": old,
        "value": value,
    }


def parsed(value=108):
    return {
        "meta": {"doc_type": "RPD"},
        "parse_issues": [],
        "disciplines": [{"name": "Math", "hours_total": value, "source_ref": {"row": 2}}],
    }


def contract_parsed(value=108):
    return {
        "meta": {
            "schema_version": 2,
            "doc_type": "RPD",
            "file_name": "РПД.docx",
            "file_hash": "a" * 64,
            "parser_config": "test",
            "program_id": "test-program",
        },
        "disciplines": [
            {
                "name": "Math",
                "semesters": [1],
                "credit_units": 3,
                "hours_total": value,
                "hours_by_kind": {"лекции": value},
                "source_ref": {"file": "РПД.docx", "paragraph_index": 1},
            }
        ],
        "parse_issues": [],
    }


def test_selected_values_apply_to_a_fresh_copy_of_original():
    original = {DOC: parsed()}
    changes = [mutation("/disciplines/0/hours_total", 108, 144)]

    selected = apply_document_mutations(original, changes)

    assert selected[DOC]["disciplines"][0]["hours_total"] == 144
    assert original[DOC]["disciplines"][0]["hours_total"] == 108


def test_null_is_a_present_value_and_boolean_is_not_number():
    original = {DOC: parsed(None)}
    assert (
        apply_document_mutations(original, [mutation("/disciplines/0/hours_total", None, 0)])[DOC][
            "disciplines"
        ][0]["hours_total"]
        == 0
    )

    with pytest.raises(DomainError):
        apply_document_mutations(
            {DOC: parsed(True)}, [mutation("/disciplines/0/hours_total", 1, 2)]
        )


def test_missing_path_is_not_treated_as_null():
    with pytest.raises(DomainError, match="отсутствует"):
        apply_document_mutations({DOC: parsed()}, [mutation("/disciplines/0/missing", None, 1)])


@pytest.mark.parametrize(
    "path",
    [
        "/meta",
        "/meta/doc_type",
        "/parse_issues/0/message",
        "/disciplines/0/source_ref",
        "/disciplines/0",
        "/disciplines",
    ],
)
def test_protected_fields_and_parent_replacement_are_rejected(path):
    value = (
        parsed()["meta"]["doc_type"]
        if path.startswith("/meta")
        else (
            parsed()["disciplines"]
            if path == "/disciplines"
            else parsed()["disciplines"][0]
            if path == "/disciplines/0"
            else parsed()["disciplines"][0]["source_ref"]
        )
    )
    with pytest.raises((DomainError, ValidationError)):
        validate_document_mutations({DOC: parsed()}, [mutation(path, value, {})])


def test_overlapping_or_conflicting_choices_fail_as_a_whole():
    original = {DOC: parsed()}
    with pytest.raises(DomainError):
        apply_document_mutations(
            original,
            [
                mutation("/disciplines/0/hours_total", 108, 144),
                mutation("/disciplines/0", original[DOC]["disciplines"][0], {"name": "Other"}),
            ],
        )
    with pytest.raises(DomainError):
        apply_document_mutations(
            original,
            [
                mutation("/disciplines/0/hours_total", 108, 144),
                mutation("/disciplines/0/hours_total", 108, 180),
            ],
        )


def test_pointer_escaping_and_array_indices_are_applied():
    document = {DOC: {"a/b": {"~key": [None, "old"]}}}
    selected = apply_document_mutations(
        document,
        [mutation("/a~1b/~0key/1", "old", "new")],
    )
    assert selected[DOC]["a/b"]["~key"] == [None, "new"]


def test_identical_selected_values_on_the_same_path_are_applied_once():
    original = {DOC: parsed()}
    change = mutation("/disciplines/0/hours_total", 108, 144)

    selected = apply_document_mutations(original, [change, change])

    assert selected[DOC]["disciplines"][0]["hours_total"] == 144
    assert original[DOC]["disciplines"][0]["hours_total"] == 108


def test_pointer_array_index_must_use_ascii_decimal_digits():
    with pytest.raises(DomainError, match="индекс"):
        apply_document_mutations(
            {DOC: {"items": ["old"]}},
            [mutation("/items/٠", "old", "new")],
        )


def test_mutation_cannot_introduce_a_protected_field():
    with pytest.raises(DomainError, match="защищённые"):
        validate_document_mutations(
            {DOC: parsed()},
            [mutation("/disciplines/0/name", "Math", {"source_ref": {"row": 2}})],
        )


def test_each_option_must_still_satisfy_the_document_contract():
    original = {DOC: contract_parsed()}

    with pytest.raises(DomainError, match="контракту"):
        validate_document_mutation_variants(
            original,
            [[mutation("/disciplines/0/hours_total", 108, "не число")]],
        )

    validate_document_mutation_variants(
        original,
        [[mutation("/disciplines/0/hours_total", 108, 144)]],
    )
