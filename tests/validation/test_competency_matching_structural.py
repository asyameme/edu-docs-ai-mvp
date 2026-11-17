"""Regression coverage for R-01 matching and R-08 document-local references."""
from copy import deepcopy
from worker.validation.validator import load_rules, run_validation

def rule(rule_id):
    config = load_rules()
    return {**config, 'rules': [deepcopy(next(r for r in config['rules'] if r['id'] == rule_id))]}


R01 = rule('R-01')
R08 = rule('R-08-local-reference')


def doc(kind, name, pid='P', **fields):
    return {'meta': {'doc_type': kind, 'program_id': pid, 'file_name': name}, **fields}


def test_r01_candidate_preserves_both_values_and_sources():
    a = doc('matrix', 'matrix.json', competencies=[{'code': 'УНК-03', 'formulation': 'Способен применять методы стратегического планирования', 'source_ref': {'section': 'section A'}}])
    b = doc('rpd', 'rpd.json', competencies=[{'code': 'ПКП-УНК-03', 'formulation': 'Способен применять методы стратегического планирования', 'source_ref': {'section': 'section B'}}])
    old = deepcopy((a, b))
    res = run_validation([a, b], R01).results[0]
    matches = [f for f in res.findings if f.code == 'competency_code_match_candidate']
    assert len(matches) == 1
    finding = matches[0]
    assert finding.status == 'needs_review'
    assert finding.evidence['requires_user_confirmation'] is True
    assert finding.evidence['auto_merge'] is False
    assert finding.evidence['codes'] == ['ПКП-УНК-03', 'УНК-03']
    assert {r['section'] for r in finding.evidence['source_refs']} == {'section A', 'section B'}
    assert (a, b) == old


def test_r01_does_not_merge_same_suffix_when_formulations_differ():
    a = doc('matrix', 'matrix.json', competencies=[{'code': 'УНК-03', 'formulation': 'Способен проектировать интерфейсы'}])
    b = doc('rpd', 'rpd.json', competencies=[{'code': 'ПКП-УНК-03', 'formulation': 'Способен проектировать структуры данных'}])
    res = run_validation([a, b], R01).results[0]
    assert not any(f.code == 'competency_code_match_candidate' for f in res.findings)


def test_r01_different_programs_not_matched():
    a = doc('matrix', 'm', pid='P', competencies=[{'code': 'A', 'formulation': 'Same'}])
    b = doc('rpd', 'r', pid='Q', competencies=[{'code': 'B', 'formulation': 'Same'}])
    res = run_validation([a,b], R01).results[0]
    assert not any(f.code == 'competency_code_match_candidate' for f in res.findings)


def test_r08_indicator_missing_competency_fails():
    rpd = doc('rpd', 'rpd.json', competencies=[{'code': 'УК-1'}],
              indicators=[{'code': 'ИУК-2.1', 'competency_code': 'УК-2', 'source_ref': {'section': 'indicators'}}])
    res = run_validation([rpd], R08).results[0]
    assert res.status == 'failed'
    assert res.findings[0].code == 'dangling_reference'
    assert res.findings[0].evidence['source_refs'][0]['section'] == 'indicators'


def test_r08_indicator_existing_competency_passes():
    rpd = doc('rpd', 'rpd.json', competencies=[{'code': 'УК-1'}], indicators=[{'code': 'ИУК-1.1', 'competency_code': 'УК-1'}])
    assert run_validation([rpd], R08).results[0].status == 'passed'


def test_r01_collects_all_supporting_document_variants():
    formulation = 'Способен применять методы стратегического планирования'
    documents = [
        doc('matrix', 'matrix.json', competencies=[{'code': 'УНК-03', 'formulation': formulation}]),
        doc('rpd', 'rpd.json', competencies=[{'code': 'ПКП-УНК-03', 'formulation': formulation}]),
        doc('rpp', 'rpp.json', competencies=[{'code': 'ПКП-УНК-03', 'formulation': formulation}]),
    ]
    finding = next(f for f in run_validation(documents, R01).results[0].findings
                   if f.code == 'competency_code_match_candidate')
    assert len(finding.evidence['variants']) == 3
    assert len(finding.evidence['evidence_pairs']) == 2
    assert {v['source_ref']['file'] for v in finding.evidence['variants']} == {
        'matrix.json', 'rpd.json', 'rpp.json'}


def test_r01_threshold_is_configurable_without_auto_matching_codes():
    a = doc('matrix', 'a', competencies=[{'code': 'A', 'formulation': 'Способен проводить анализ данных'}])
    b = doc('rpd', 'b', competencies=[{'code': 'B', 'formulation': 'Способен проводить анализ больших данных'}])
    result = run_validation([a, b], {**R01, 'rules': [{**R01['rules'][0], 'match_threshold': 0.6}]}).results[0]
    candidates = [f for f in result.findings if f.code == 'competency_code_match_candidate']
    assert len(candidates) == 1
    assert candidates[0].evidence['heuristic_only']
    assert candidates[0].evidence['auto_merge'] is False


def test_r01_does_not_propose_codes_coexisting_in_one_document():
    a = doc('matrix', 'a', competencies=[{'code': 'A', 'formulation': 'Одна формулировка'},
                                         {'code': 'B', 'formulation': 'Одна формулировка'}])
    b = doc('rpd', 'b', competencies=[{'code': 'B', 'formulation': 'Одна формулировка'}])
    result = run_validation([a, b], R01).results[0]
    assert not any(f.code == 'competency_code_match_candidate' for f in result.findings)
