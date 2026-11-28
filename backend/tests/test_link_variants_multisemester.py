"""Свой вариант и варианты связей для дисциплин на несколько семестров."""

from uuid import uuid4

from app.document_analysis import _competency_link_variants

ISSUE = uuid4()


def _ref(file, row, paragraph):
    return {"file": file, "table": None, "row": row, "paragraph_index": paragraph}


def _plan_document():
    """Учебный план: одна дисциплина на два семестра, в каждой строке УК-3."""
    document = {
        "meta": {
            "schema_version": 2,
            "doc_type": "CURRICULUM",
            "file_name": "Учебный план.docx",
            "file_hash": "a" * 64,
            "parser_config": "compatibility",
            "program_id": "26/5003/1",
        },
        "curriculum_items": [
            {
                "source_ref": _ref("Учебный план.docx", 5, 101),
                "discipline_name": "Технологии MLOps",
                "semester": 5,
                "credit_units": 3,
                "hours_total": 108,
                "block": "Б1",
            },
            {
                "source_ref": _ref("Учебный план.docx", 6, 202),
                "discipline_name": "Технологии MLOps",
                "semester": 6,
                "credit_units": 3,
                "hours_total": 108,
                "block": "Б1",
            },
        ],
        "competencies": [
            {
                "code": "УК-3",
                "formulation": "Способен планировать",
                "source_ref": _ref("Учебный план.docx", 2, 12),
            },
        ],
        "relations": [
            {
                "kind": "формирует",
                "src_entity": "curriculum_item",
                "src_key": "Технологии MLOps::semester=5",
                "dst_entity": "competency",
                "dst_key": "УК-3",
                "source_ref": _ref("Учебный план.docx", 5, 101),
            },
            {
                "kind": "формирует",
                "src_entity": "curriculum_item",
                "src_key": "Технологии MLOps::semester=6",
                "dst_entity": "competency",
                "dst_key": "УК-3",
                "source_ref": _ref("Учебный план.docx", 6, 202),
            },
        ],
    }
    return document


def _rpd_document():
    """РПД той же дисциплины: список шире плана — УК-1 и УК-2 вместо УК-3."""
    ref = _ref("С5_ТОП_Технологии MLOps.docx", 1, 10)
    document = {
        "meta": {
            "schema_version": 2,
            "doc_type": "RPD",
            "file_name": "С5_ТОП_Технологии MLOps.docx",
            "file_hash": "b" * 64,
            "parser_config": "compatibility",
            "program_id": "26/5003/1",
        },
        "disciplines": [
            {
                "source_ref": ref,
                "name": "Технологии MLOps",
                "semesters": [5, 6],
                "credit_units": 6,
                "hours_total": 216,
                "hours_by_kind": {"лекции": 40},
                "control_forms": ["зачёт"],
                "prerequisites": [],
            }
        ],
        "competencies": [
            {
                "code": "УК-1",
                "formulation": "Способен А",
                "source_ref": _ref("С5_ТОП_Технологии MLOps.docx", 2, 21),
            },
            {
                "code": "УК-2",
                "formulation": "Способен Б",
                "source_ref": _ref("С5_ТОП_Технологии MLOps.docx", 2, 22),
            },
        ],
        "relations": [
            {
                "kind": "формирует",
                "src_entity": "discipline",
                "src_key": "Технологии MLOps",
                "dst_entity": "competency",
                "dst_key": code,
                "source_ref": _ref("С5_ТОП_Технологии MLOps.docx", 2, 21 + index),
            }
            for index, code in enumerate(("УК-1", "УК-2"))
        ],
    }
    return document


def _finding(plan_id, rpd_id):
    return {
        "code": "competency_relation_mismatch",
        "evidence": {
            "document_target": {
                "document_id": rpd_id, "collection": "disciplines",
                "record_index": 0, "expected_codes": ["УК-1", "УК-2"],
            },
            "plan_targets": [
                {"document_id": plan_id, "collection": "curriculum_items",
                 "record_index": 0, "expected_codes": ["УК-3"]},
                {"document_id": plan_id, "collection": "curriculum_items",
                 "record_index": 1, "expected_codes": ["УК-3"]},
            ],
            "shared_discipline": False,
            "document_label": "РПД",
            "plan_row_is_exclusive": True,
        },
    }


def _labels(variants):
    return [variant["label"] for variant in variants]


def test_rpd_list_is_offered_for_a_two_semester_discipline():
    plan_id, rpd_id = str(uuid4()), str(uuid4())
    documents = [
        {"document_id": plan_id, "parsed": _plan_document()},
        {"document_id": rpd_id, "parsed": _rpd_document()},
    ]

    variants, errors = _competency_link_variants(
        _finding(plan_id, rpd_id), documents, ISSUE
    )

    assert errors == []
    labels = _labels(variants)
    assert any(label.startswith("Список из РПД") for label in labels)
    assert any(label.startswith("Список из учебного плана") for label in labels)
    assert any(label == "Оставить расхождение в документах" for label in labels)

    from_document = next(v for v in variants if v["label"].startswith("Список из РПД"))
    assert len(from_document["changes"]) == 2
    assert sorted(change["record_index"] for change in from_document["changes"]) == [0, 1]
    assert all(change["codes"] == ["УК-1", "УК-2"] for change in from_document["changes"])

    from_plan = next(v for v in variants if v["label"].startswith("Список из учебного плана"))
    assert len(from_plan["changes"]) == 1
    assert from_plan["changes"][0]["codes"] == ["УК-3"]
    assert from_plan["changes"][0]["document_id"] == rpd_id


def test_unknown_codes_are_not_offered_for_the_plan():
    plan_id, rpd_id = str(uuid4()), str(uuid4())
    rpd = _rpd_document()
    # УК-9 упомянута в связи, но не объявлена ни в одном каталоге компетенций:
    # применить её к плану нельзя.
    rpd["relations"].append(
        {
            "kind": "формирует",
            "src_entity": "discipline",
            "src_key": "Технологии MLOps",
            "dst_entity": "competency",
            "dst_key": "УК-9",
            "source_ref": _ref("С5_ТОП_Технологии MLOps.docx", 2, 23),
        }
    )
    documents = [
        {"document_id": plan_id, "parsed": _plan_document()},
        {"document_id": rpd_id, "parsed": rpd},
    ]
    finding = _finding(plan_id, rpd_id)
    finding["evidence"]["document_target"]["expected_codes"] = ["УК-1", "УК-2", "УК-9"]

    variants, errors = _competency_link_variants(finding, documents, ISSUE)

    assert errors == []
    assert all(
        not variant["label"].startswith("Список из РПД") for variant in variants
    )
    assert any(variant["label"].startswith("Список из учебного плана") for variant in variants)
