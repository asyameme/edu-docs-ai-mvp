"""Свой вариант решения: контракт запроса и сборка изменений из шаблона."""

from uuid import uuid4

import pytest

from app.errors import DomainError
from app.review_contract import DecisionCreate
from app.review_logic import custom_decision_changes

DETAILS = {
    "requires_decision": True,
    "variants": [
        {
            "id": str(uuid4()),
            "label": "Выбрать значение из документов: Способен А",
            "changes": [
                {
                    "op": "set_document_value",
                    "document_id": str(uuid4()),
                    "field_path": "/indicators/3/text",
                    "expected_old_value": "способен А",
                    "value": "Способен А",
                }
            ],
        },
        {
            "id": str(uuid4()),
            "label": "Выбрать значение из документов: Способен Б",
            "changes": [
                {
                    "op": "set_document_value",
                    "document_id": "00000000-0000-4000-8000-000000000001",
                    "field_path": "/indicators/3/text",
                    "expected_old_value": "способен А",
                    "value": "Способен Б",
                }
            ],
        },
    ],
}


def test_contract_accepts_variant_or_custom_but_not_both():
    conflict, variant = uuid4(), uuid4()

    assert DecisionCreate(
        expected_version_id=uuid4(), conflict_id=conflict, variant_id=variant, signature="И А"
    )
    assert DecisionCreate(
        expected_version_id=uuid4(), conflict_id=conflict, custom_value="  Свой текст  ",
        signature="И А",
    ).custom_value == "Свой текст"

    for body in (
        {"variant_id": variant, "custom_value": "Свой текст"},
        {},
    ):
        with pytest.raises(Exception, match="готовый вариант или собственное значение"):
            DecisionCreate(
                expected_version_id=uuid4(), conflict_id=conflict, signature="И А", **body
            )

    with pytest.raises(Exception, match="не должно быть пустым"):
        DecisionCreate(
            expected_version_id=uuid4(), conflict_id=conflict, custom_value="   ", signature="И А"
        )


def test_custom_changes_use_the_first_variant_as_template():
    changes = custom_decision_changes(DETAILS, "Способен В", rule_code="R-08-semantic")

    assert [change["value"] for change in changes] == ["Способен В"]
    assert changes[0]["expected_old_value"] == "способен А"
    assert changes[0]["field_path"] == "/indicators/3/text"


def test_custom_competency_code_replaces_single_difference_in_both_lists():
    links = {
        "requires_decision": True,
        "variants": [
            {
                "id": str(uuid4()),
                "label": "Список из учебного плана",
                "changes": [
                    {
                        "op": "set_competency_links",
                        "document_id": str(uuid4()),
                        "collection": "curriculum_items",
                        "record_index": 0,
                        "expected_codes": ["ОПК-1", "ПКП-ППК-ДА1"],
                        "codes": ["ОПК-1", "ПКП-УНК-03"],
                    }
                ],
            },
            {
                "id": str(uuid4()),
                "label": "Список из РПД",
                "changes": [
                    {
                        "op": "set_competency_links",
                        "document_id": str(uuid4()),
                        "collection": "disciplines",
                        "record_index": 1,
                        "expected_codes": ["ОПК-1", "ПКП-УНК-03"],
                        "codes": ["ОПК-1", "ПКП-ППК-ДА1"],
                    }
                ],
            }
        ],
    }
    changes = custom_decision_changes(links, "ПКП-ППК-ДА2", rule_code="R-01-graph")

    assert [change["codes"] for change in changes] == [
        ["ОПК-1", "ПКП-ППК-ДА2"],
        ["ОПК-1", "ПКП-ППК-ДА2"],
    ]
    assert [change["expected_codes"] for change in changes] == [
        ["ОПК-1", "ПКП-ППК-ДА1"],
        ["ОПК-1", "ПКП-УНК-03"],
    ]


def test_custom_competency_code_adds_extra_code_to_other_list():
    links = {
        "variants": [
            {
                "label": "Список из учебного плана",
                "changes": [{
                    "op": "set_competency_links", "document_id": str(uuid4()),
                    "collection": "curriculum_items", "record_index": 0,
                    "expected_codes": ["ОПК-1", "ПКП-ППК-ДА1"],
                    "codes": ["ОПК-1"],
                }],
            },
            {
                "label": "Список из РПД",
                "changes": [{
                    "op": "set_competency_links", "document_id": str(uuid4()),
                    "collection": "disciplines", "record_index": 0,
                    "expected_codes": ["ОПК-1"], "codes": ["ОПК-1", "ПКП-ППК-ДА1"],
                }],
            },
        ],
    }
    changes = custom_decision_changes(links, "ПКП-ППК-ДА2", rule_code="R-01-graph")

    assert [change["codes"] for change in changes] == [
        ["ОПК-1", "ПКП-ППК-ДА2"],
        ["ОПК-1", "ПКП-ППК-ДА2"],
    ]


def test_custom_competency_code_requires_one_difference_and_one_target_per_source():
    links = {
        "variants": [
            {
                "label": "Список из учебного плана",
                "changes": [{
                    "op": "set_competency_links", "document_id": str(uuid4()),
                    "collection": "curriculum_items", "record_index": 0,
                    "expected_codes": ["ОПК-1", "ПКП-1", "ПКП-2"], "codes": ["ОПК-1"],
                }],
            },
            {
                "label": "Список из РПД",
                "changes": [{
                    "op": "set_competency_links", "document_id": str(uuid4()),
                    "collection": "disciplines", "record_index": 0,
                    "expected_codes": ["ОПК-1", "ПКП-3"], "codes": ["ОПК-1"],
                }],
            },
        ],
    }
    with pytest.raises(DomainError, match="отличается не больше одного кода"):
        custom_decision_changes(links, "ПКП-1", rule_code="R-01-graph")
    with pytest.raises(DomainError, match="без пробелов"):
        custom_decision_changes(links, "ПКП 1", rule_code="R-01-graph")


def test_custom_changes_remain_limited_for_other_rules():
    links = {
        "variants": [{"changes": [{"op": "set_competency_links"}]}],
    }
    with pytest.raises(DomainError, match="только для замены текста"):
        custom_decision_changes(links, "Свой текст", rule_code="R-08-semantic")

    with pytest.raises(DomainError, match="только для правил формулировок"):
        custom_decision_changes(DETAILS, "Свой код", rule_code="R-04")

    with pytest.raises(DomainError, match="нет шаблона полей"):
        custom_decision_changes({"variants": []}, "Свой текст", rule_code="R-08-semantic")
