"""Развернутые пояснения к предупреждениям нагрузки."""

from worker.validation.validator import Document, _workload_explanation


def test_hours_mismatch_explains_plan_and_document_numbers():
    load_row = {"hours_by_kind": {"лекции": 40, "практические занятия": 24, "семинары": 44}}

    explanation = _workload_explanation(
        "Интеллектуальные системы", 7, "hours_total", 144, 108, "rpd", load_row
    )

    assert "семестра 7" in explanation
    assert "144" in explanation and "108" in explanation
    assert "Расхождение: 36" in explanation
    # Число 108 не написано в документе одной ячейкой — показываем, из чего оно складывается.
    assert (
        "лекции 40 + практические занятия 24 + семинары 44 = 108" in explanation
    )
    assert "править нужно графы, а не итог" in explanation


def test_non_hours_and_unknown_kinds():
    explanation = _workload_explanation("Дисциплина", 3, "credit_units", 4, 3, "rpp", {})

    assert "4 credit_units" in explanation and "Расхождение: 1" in explanation
    plain = _workload_explanation(
        "Дисциплина", 3, "hours_total", 144, 72, "rpd", {"чужое": "текст"}
    )
    assert "72" in plain


def test_r04_credit_hours_and_r05_title_explanations():
    # Проверяем композитор R-04 через реальный прогон правила на мини-документах.
    from worker.validation.validator import _credit_hours

    rule = {"id": "R-04", "inputs": {"hours_per_credit_unit": 36}}
    documents = [
        Document(
            data={
                "meta": {"doc_type": "RPD", "file_name": "РПД.docx"},
                "disciplines": [{
                    "source_ref": {"file": "РПД.docx", "table": 2, "row": 6},
                    "hours_total": 154, "credit_units": 4,
                }],
            },
            label="С5_076083_Глубокое обучение.docx",
            document_id="0c0c0000-0000-4000-8000-000000000001",
        )
    ]
    result = _credit_hours(rule, documents)
    mismatch = next(f for f in result.findings if f.code == "credit_hours_mismatch")

    assert "144" in mismatch.evidence["explanation"]
    assert "154" in mismatch.evidence["explanation"]
    assert "10 ч" in mismatch.evidence["explanation"]


def test_r05_title_mismatch_explains_both_numbers():
    from worker.validation.validator import _title_credit_units

    rule = {"id": "R-05-title"}
    documents = [
        Document(
            data={
                "meta": {"doc_type": "RPD", "file_name": "РПД.docx"},
                "title_credit_units": 7,
                "title_credit_units_source_ref": {"file": "РПД.docx", "table": 1, "row": 1},
                "disciplines": [{
                    "source_ref": {"file": "РПД.docx", "table": 2, "row": 6},
                    "credit_units": 6,
                }],
            },
            label="С7-8_Стандарт_058776_Интернет вещей.docx",
            document_id="0c0c0000-0000-4000-8000-000000000002",
        )
    ]
    result = _title_credit_units(rule, documents)
    mismatch = next(f for f in result.findings if f.code == "title_credit_units_mismatch")

    assert "на титуле" in mismatch.evidence["explanation"]
    assert "7" in mismatch.evidence["explanation"] and "6" in mismatch.evidence["explanation"]
    assert "Расхождение: 1 ЗЕТ" in mismatch.evidence["explanation"]
