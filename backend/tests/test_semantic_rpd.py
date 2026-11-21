from worker.validation.semantic_rpd import evaluate_rpd_alignment


def test_r11_uses_section_text_and_batches_large_outcome_sets():
    outcomes = [
        {"local_id": f"LO-{index:03d}", "text": f"Результат {index}"}
        for index in range(1, 22)
    ]
    relations = [
        {
            "kind": "формирует",
            "src_entity": "discipline",
            "src_key": "Дисциплина",
            "dst_entity": "competency",
            "dst_key": "C1",
        },
        {
            "kind": "имеет",
            "src_entity": "competency",
            "src_key": "C1",
            "dst_entity": "indicator",
            "dst_key": "I1",
        },
        {
            "kind": "проверяется",
            "src_entity": "indicator",
            "src_key": "I1",
            "dst_entity": "assessment_tool",
            "dst_key": "Экзамен",
        },
        *[
            {
                "kind": "включает",
                "src_entity": "competency",
                "src_key": "C1",
                "dst_entity": "learning_outcome",
                "dst_key": outcome["local_id"],
            }
            for outcome in outcomes
        ],
    ]
    section_ref = {"file": "RPD.docx", "paragraph_index": 42, "section": "2.2"}
    calls = []

    def evaluator(payload, _config):
        calls.append(payload)
        return {
            "results": [
                {
                    "outcome_id": outcome["id"],
                    "status": "gap" if outcome["id"] == "LO-001" else "supported",
                    "course_evidence_ids": ["C001"],
                    "assessment_evidence_ids": ["A001"],
                    "reason": "Нужно проверить связь результата с содержанием.",
                }
                for outcome in payload["learning_outcomes"]
            ]
        }

    checks, findings = evaluate_rpd_alignment(
        document_label="RPD.docx",
        disciplines=[{"name": "Дисциплина"}],
        outcomes=outcomes,
        course_content=[],
        course_content_sections=[
            {
                "section_number": "2.2",
                "title": "Структура и содержание учебных занятий",
                "text": "Текст содержания занятий и тем.",
                "source_ref": section_ref,
            }
        ],
        indicators=[{"code": "I1", "text": "Индикатор", "competency_code": "C1"}],
        assessment_tools=[{"name": "Экзамен"}],
        relations=relations,
        llm_config={"max_outcomes_per_request": 20},
        evaluator=evaluator,
    )

    assert checks == 21
    assert [len(call["learning_outcomes"]) for call in calls] == [20, 1]
    assert all(
        call["course_content_section_2"][0]["topic"] == "Текст содержания занятий и тем."
        for call in calls
    )
    assert [finding["code"] for finding in findings] == ["rpd_semantic_alignment_gap"]
    assert {
        "file": "RPD.docx",
        "paragraph_index": 42,
        "section": "2.2",
    } in findings[0]["evidence"]["source_refs"]
