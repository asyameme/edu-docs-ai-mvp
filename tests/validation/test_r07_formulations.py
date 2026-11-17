"""R-07 regression tests: no normative baseline and no automatic resolution."""
from worker.validation.validator import load_rules, run_validation

RULES = load_rules()
RULE = {**RULES, 'rules': [next(r for r in RULES['rules'] if r['id'] == 'R-07')]}


def doc(kind, name, code='УК-1', formulation='Исходная формулировка', pid='p'):
    return {'meta': {'doc_type': kind, 'file_name': name, 'program_id': pid},
            'competencies': [{'code': code, 'formulation': formulation,
                              'source_ref': {'section': 'competencies'}}]}


def test_compares_without_ckmv_or_characteristic():
    calls = []
    def llm(payload, config):
        calls.append(payload)
        return {'status': 'changed', 'reason': 'Добавлен уровень.'}
    result = run_validation([
        doc('rpd', 'a.json', formulation='Способен решать задачи.'),
        doc('rpp', 'b.json', formulation='Способен решать задачи на продвинутом уровне.'),
    ], RULE, competency_evaluator=llm).results[0]
    assert result.status == 'needs_review'
    assert result.checks == 1
    assert len(calls) == 1
    assert len(result.findings) == 1
    finding = result.findings[0]
    assert finding.status == 'needs_review'
    assert finding.evidence['comparisons'][0]['status'] == 'changed'
    assert finding.evidence['requires_user_confirmation'] is True
    assert [v['document'] for v in finding.evidence['variants']] == ['a.json', 'b.json']
    assert all(v['source_ref']['section'] == 'competencies' for v in finding.evidence['variants'])


def test_ckmv_not_assumed_as_approved_reference():
    result = run_validation([
        doc('rpd', 'rpd.json', formulation='A'),
        doc('ckmv', 'ckmv.json', formulation='B'),
        doc('characteristic', 'characteristic.json', formulation='C'),
    ], RULE, competency_evaluator=lambda *_: {'status': 'changed', 'reason': 'Разные варианты'}).results[0]
    assert result.checks == 3  # All 3 unordered pairs, not a CKMV baseline.
    assert len(result.findings) == 1
    assert len(result.findings[0].evidence['comparisons']) == 3
    assert all(f.status == 'needs_review' for f in result.findings)
    assert not any('reference_text' in f.evidence for f in result.findings)


def test_technical_differences_do_not_require_review():
    result = run_validation([doc('rpd', 'a', formulation='  ТЕКСТ   компетенции. '),
                             doc('rpp', 'b', formulation='текст компетенции')], RULE,
                            competency_evaluator=lambda *_: (_ for _ in ()).throw(AssertionError('No LLM needed'))).results[0]
    assert result.status == 'passed'
    assert result.checks == 1


def test_unavailable_llm_requests_manual_review():
    result = run_validation([doc('rpd', 'a', formulation='Первый'), doc('rpp', 'b', formulation='Второй')],
                            RULE, competency_evaluator=lambda *_: (_ for _ in ()).throw(RuntimeError('offline'))).results[0]
    assert result.status == 'needs_review'
    assert result.findings[0].evidence['comparisons'][0]['status'] == 'uncertain'


def test_different_codes_not_matched_in_r07():
    result = run_validation([doc('rpd', 'a', code='УНК-03'),
                             doc('rpp', 'b', code='ПКП-УНК-03')], RULE).results[0]
    assert result.checks == 0
    assert result.status == 'skipped'


def test_missing_formulation_and_program_boundaries():
    result = run_validation([doc('rpd', 'a', formulation=''),
                             doc('rpp', 'b', formulation='Непустая'),
                             doc('rpd', 'other', pid='another')], RULE).results[0]
    assert result.checks == 1
    assert result.findings[0].code == 'competency_formulation_missing'
    assert result.findings[0].status == 'needs_review'


def test_semantically_equivalent_but_different_wording_still_reviewed():
    result = run_validation([doc('rpd', 'a', formulation='Умеет работать с данными.'),
                             doc('rpp', 'b', formulation='Способен работать с данными.')], RULE,
                            competency_evaluator=lambda *_: {'status': 'equivalent', 'reason': 'Смысл одинаковый'}).results[0]
    assert result.status == 'needs_review'
    assert result.findings[0].evidence['comparisons'][0]['status'] == 'equivalent'


def test_three_documents_result_in_single_review_with_every_pair_and_source():
    files = [doc('rpd', 'one.json', formulation='Способен анализировать данные'),
             doc('rpp', 'two.json', formulation='Способен анализировать большие данные'),
             doc('matrix', 'three.json', formulation='Способен анализировать текстовые данные')]
    result = run_validation(files, RULE, competency_evaluator=lambda *_: {'status':'changed','reason':'Изменен объект'}).results[0]
    assert result.checks == 3
    assert len(result.findings) == 1
    assert len(result.findings[0].evidence['variants']) == 3
    assert len(result.findings[0].evidence['comparisons']) == 3
    assert {ref['file'] for ref in result.findings[0].evidence['source_refs']} == {'one.json','two.json','three.json'}


def test_llm_inputs_do_not_assign_authoritative_reference():
    inputs = []
    def model(payload, _):
        inputs.append(payload)
        return {'status':'changed','reason':'Есть различия'}
    run_validation([doc('ckmv','a',formulation='Текст 1'), doc('rpd','b',formulation='Текст 2')],
                   RULE, competency_evaluator=model)
    assert set(inputs[0]) == {'competency_code', 'variant_a', 'variant_b'}
