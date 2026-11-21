"""R-08 deterministic grouping and choice preparation without an LLM."""
from worker.validation.validator import load_rules, run_validation

RULES = load_rules()
RULE = {**RULES, 'rules': [next(r for r in RULES['rules'] if r['id'] == 'R-08-semantic')]}


def doc(kind, name, text, *, code='УК-2.1', program='p'):
    return {
        'meta': {'doc_type': kind, 'file_name': name, 'program_id': program},
        'indicators': [{'code': code, 'text': text,
                        'source_ref': {'file': name, 'section': 'indicators'}}],
    }


def test_different_normalized_formulations_create_user_choice():
    calls = []

    def unexpected_llm_call(*args, **kwargs):
        calls.append((args, kwargs))
        raise AssertionError('R-08 must not call an LLM')

    result = run_validation([
        doc('rpd', 'a.json', 'Анализирует данные.'),
        doc('rpp', 'b.json', 'Обрабатывает данные'),
    ], RULE, semantic_evaluator=unexpected_llm_call).results[0]

    assert result.status == 'needs_review'
    assert len(result.findings) == 1
    finding = result.findings[0]
    assert finding.code == 'indicator_formulation_review'
    assert finding.evidence['requires_user_confirmation'] is True
    assert [variant['text'] for variant in finding.evidence['variants']] == [
        'Анализирует данные.', 'Обрабатывает данные'
    ]
    assert len(finding.evidence['source_refs']) == 2
    assert 'model_status' not in finding.evidence
    assert 'model_reason' not in finding.evidence
    assert calls == []


def test_case_whitespace_and_terminal_punctuation_are_normalized():
    result = run_validation([
        doc('rpd', 'a.json', '  АНАЛИЗИРУЕТ   данные. '),
        doc('rpp', 'b.json', 'анализирует данные'),
    ], RULE).results[0]

    assert result.status == 'passed'
    assert result.findings == []


def test_yo_and_common_abbreviation_differences_do_not_require_choice():
    result = run_validation([
        doc('rpd', 'a.json', 'Развёртывает сервисы, в том числе в облаке.'),
        doc('rpp', 'b.json', 'Развертывает сервисы, в т. ч. в облаке'),
    ], RULE).results[0]

    assert result.status == 'passed'
    assert result.findings == []


def test_same_code_is_grouped_once_across_all_documents():
    result = run_validation([
        doc('rpd', 'a.json', 'Текст А'),
        doc('rpp', 'b.json', 'Текст Б'),
        doc('ckmv', 'c.json', 'Текст В'),
    ], RULE, phase='graph_validation').results[0]

    assert result.checks == 1
    assert len(result.findings) == 1
    assert len(result.findings[0].evidence['variants']) == 3


def test_different_programs_are_not_compared():
    result = run_validation([
        doc('rpd', 'a.json', 'Текст А', program='p1'),
        doc('rpp', 'b.json', 'Текст Б', program='p2'),
    ], RULE).results[0]

    assert result.status == 'skipped'
    assert result.findings == []
