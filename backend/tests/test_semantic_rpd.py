import pytest

from worker.validation.semantic_rpd import evaluate_rpd_alignment


@pytest.mark.parametrize(
    ("outcome_count", "expected_batch_sizes"),
    [(21, [20, 1]), (75, [25, 25, 25])],
)
def test_r11_uses_section_text_and_caps_one_document_at_three_requests(
    outcome_count, expected_batch_sizes,
):
    outcomes = [
        {"local_id": f"LO-{index:03d}", "text": f"Результат {index}"}
        for index in range(1, outcome_count + 1)
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
        llm_config={"max_outcomes_per_request": 20, "max_requests_per_document": 3},
        evaluator=evaluator,
    )

    assert checks == outcome_count
    assert [len(call["learning_outcomes"]) for call in calls] == expected_batch_sizes
    assert len(calls) <= 3
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


@pytest.mark.parametrize('status,heading', [
    ('gap', 'Возможное смысловое несоответствие'),
    ('uncertain', 'Недостаточно сведений'),
])
def test_r11_message_contains_readable_evidence(status, heading):
    from worker.validation.semantic_rpd import _alignment_message

    details = {
        'course_evidence': [{'topic': 'Числовые последовательности', 'activity': 'Лекции'}],
        'assessment_evidence': [{'assessment_tool': 'Экзамен', 'indicator_code': 'УК-1.1',
                                 'indicator_text': 'Анализирует задачи'}],
        'reason': 'Проверить содержание экзаменационных заданий.',
    }
    message = _alignment_message(status, {'text': 'Анализировать взаимосвязи'}, details, 'Математический анализ')
    for expected in [heading, 'Математический анализ', 'Анализировать взаимосвязи',
                     'Числовые последовательности', 'Экзамен', 'УК-1.1', details['reason'],
                     'нарушение не установлено']:
        assert expected in message


def test_r11_empty_evidence_does_not_claim_document_has_no_content():
    from worker.validation.semantic_rpd import _alignment_message

    message = _alignment_message('uncertain', {'text': 'Навык'}, {
        'course_evidence': [], 'assessment_evidence': [], 'reason': 'Недостаточно сведений.'
    }, 'РПД')
    assert 'Содержание (раздел 2): модель не указала ссылки' in message
    assert 'Оценивание (раздел 3): модель не указала ссылки' in message
    assert 'не содержит' not in message


@pytest.mark.parametrize('status,course_ids,assessment_ids,expected_code', [
    ('gap', [], [], 'rpd_semantic_alignment_gap'),
    ('uncertain', [], [], 'rpd_semantic_alignment_uncertain'),
    ('uncertain', ['C002'], [], 'rpd_semantic_alignment_uncertain'),
    ('gap', [], ['A001'], 'rpd_semantic_alignment_gap'),
    ('gap', ['C002'], ['A001'], 'rpd_semantic_alignment_gap'),
    ('supported', [], ['A001'], 'rpd_semantic_evidence_missing'),
    ('supported', ['C002'], ['A001'], None),
    ('gap', ['C999'], [], 'rpd_semantic_response_invalid'),
    ('uncertain', [], ['A002'], 'rpd_semantic_response_invalid'),
])
def test_r11_review_examples_are_not_model_evidence(
    status, course_ids, assessment_ids, expected_code,
):
    def ref(section, row):
        return {'file': 'RPD.docx', 'section': section, 'row': row}

    def relation(kind, src_entity, src_key, dst_entity, dst_key, source_ref=None):
        value = dict(kind=kind, src_entity=src_entity, src_key=src_key,
                     dst_entity=dst_entity, dst_key=dst_key)
        if source_ref:
            value['source_ref'] = source_ref
        return value

    def evaluator(payload, config):
        assert 'РАССМОТРЕННЫХ' in config['_system_prompt']
        assert 'включи их идентификаторы' in config['_system_prompt']
        return {'results': [{
            'outcome_id': 'LO-001', 'status': status,
            'course_evidence_ids': course_ids,
            'assessment_evidence_ids': assessment_ids,
            'reason': 'Тема Производные не раскрывает моделирование. Проверить экзамен.',
        }]}

    checks, findings = evaluate_rpd_alignment(
        document_label='RPD.docx', disciplines=[{'name': 'Математический анализ'}],
        outcomes=[{'local_id': 'LO-001', 'text': 'Строить модели',
                   'source_ref': ref('1.3', 2)}],
        course_content=[
            {'topic': 'Последовательности', 'source_ref': ref('2.2', 1)},
            {'topic': 'Производные', 'source_ref': ref('2.2', 2)},
            {'topic': 'Пределы', 'source_ref': ref('2.2', 3)},
            {'topic': 'Интегралы', 'source_ref': ref('2.2', 4)},
        ],
        indicators=[{'code': 'I1', 'text': 'Анализирует задачи', 'competency_code': 'C1'},
                    {'code': 'I2', 'text': 'Несвязанный индикатор', 'competency_code': 'C2'}],
        assessment_tools=[{'name': 'Экзамен'}, {'name': 'Несвязанное средство'}],
        relations=[
            relation('включает', 'competency', 'C1', 'learning_outcome', 'LO-001'),
            relation('проверяется', 'indicator', 'I1', 'assessment_tool', 'Экзамен', ref('3.1.4', 2)),
            relation('проверяется', 'indicator', 'I2', 'assessment_tool', 'Несвязанное средство', ref('3.1.4', 3)),
        ],
        llm_config={}, evaluator=evaluator,
    )
    assert checks == 1
    if expected_code is None:
        assert not findings
        return
    assert len(findings) == 1
    finding = findings[0]
    assert finding['code'] == expected_code
    assert finding['status'] == 'needs_review'
    evidence = finding['evidence']
    message = finding['message']
    assert ref('1.3', 2) in evidence['source_refs']
    if expected_code == 'rpd_semantic_response_invalid':
        assert evidence['course_evidence_ids'] == evidence['assessment_evidence_ids'] == []
        assert 'Примеры извлечённых данных' not in message
        return
    assert evidence['course_evidence_ids'] == course_ids
    assert evidence['assessment_evidence_ids'] == assessment_ids
    if status == 'supported':
        assert 'Примеры извлечённых данных' not in message
        return
    assert 'Производные' in message
    assert 'Экзамен' in message
    assert ref('2.2', 2) in evidence['source_refs']
    assert ref('3.1.4', 2) in evidence['source_refs']
    assert 'Несвязанное средство' not in message
    assert ref('3.1.4', 3) not in evidence['source_refs']
    assert 'Интегралы' not in message
    assert ('Примеры извлечённых данных' in message) == (not course_ids or not assessment_ids)
    if not course_ids:
        assert evidence['course_evidence'] == []
        assert 'Не являются свидетельствами, отобранными моделью' in message
        assert message.index('Производные') < message.index('Последовательности')
    if not assessment_ids:
        assert evidence['assessment_evidence'] == []
        assert 'Не являются свидетельствами, отобранными моделью' in message
