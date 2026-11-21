"""Pilot rules for CKMV structure, outcome paths and program languages."""
from copy import deepcopy

import pytest

from backend.worker.validation.validator import load_rules, run_validation


def doc(role, **fields):
    return {"meta": {"doc_type": role, "program_id": "P"}, **fields}


def check(rule_id, docs):
    config = load_rules()
    rule = next(r for r in config['rules'] if r['id'] == rule_id)
    rule['status'] = 'ready'
    config['rules'] = [rule]
    before = deepcopy(docs)
    result = run_validation(docs, config, phase='graph_validation').results[0]
    assert docs == before
    return result


def path_doc(role):
    return doc(role, competencies=[{'code': 'A'}], indicators=[{'code': 'A.1'}],
               learning_outcomes=[{'local_id': 'L'}], relations=[
                   {'src_entity': 'competency', 'src_key': 'A', 'dst_entity': 'indicator', 'dst_key': 'A.1', 'kind': 'имеет'},
                   {'src_entity': 'competency', 'src_key': 'A', 'dst_entity': 'learning_outcome', 'dst_key': 'L', 'kind': 'включает'},
               ])


@pytest.mark.parametrize('role', ['RPD', 'RPP'])
def test_indirect_path(role):
    data = path_doc(role)
    assert check('R-10', [data]).status == 'passed'
    data['competencies'] = []
    assert check('R-10', [data]).status == 'needs_review'


def test_ckmv_requires_direct_link_for_every_outcome():
    data = path_doc('CKMV')
    assert check('R-10', [data]).status == 'needs_review'
    data['indicator_outcome_links'] = [{'indicator_code': 'A.1', 'learning_outcome_id': 'L'}]
    assert check('R-10', [data]).status == 'passed'
    data['learning_outcomes'].append({'local_id': 'L2'})
    assert check('R-10', [data]).status == 'needs_review'
    data['indicator_outcome_links'][0]['learning_outcome_id'] = 'unknown'
    assert check('R-10', [data]).status == 'failed'


def test_sections_use_positive_source_evidence():
    rule = next(r for r in load_rules()['rules'] if r['id'] == 'R-13')
    titles = rule['inputs']['required_sections']
    data = doc('CKMV', competencies=[{'code': str(i), 'source_ref': {'section': title}} for i, title in enumerate(titles)])
    assert check('R-13', [data]).status == 'passed'
    data['competencies'].pop()
    assert check('R-13', [data]).status == 'needs_review'


def test_indicator_codes_and_order():
    def indicator(code, row):
        return {'code': code, 'competency_code': 'A', 'source_ref': {'section': 'S', 'table': 1, 'row': row}}
    data = doc('CKMV', indicators=[indicator('A.01', 1), indicator('A.3', 2)])
    assert check('R-15', [data]).status == 'passed'
    data['indicators'][1]['code'] = 'A.x'
    assert check('R-15', [data]).status == 'needs_review'
    data['indicators'][1]['code'] = 'B.2'
    assert check('R-15', [data]).status == 'failed'
    data['indicators'] = [indicator('A.3', 1), indicator('A.1', 2)]
    assert check('R-15', [data]).status == 'needs_review'
    data['indicators'][1]['source_ref']['section'] = 'T'
    assert check('R-15', [data]).status == 'passed'


def test_language_scope_and_conflicting_characteristics():
    characteristic = doc('CHARACTERISTIC', program={'education_languages': ['русский', 'английский']})
    rpd = doc('RPD', education_languages=[' Русский '])
    assert check('R-14', [characteristic, rpd]).status == 'passed'
    rpd['education_languages'] = ['французский']
    assert check('R-14', [characteristic, rpd]).status == 'failed'
    conflict = doc('CHARACTERISTIC', program={'education_languages': ['французский']})
    assert check('R-14', [characteristic, conflict, rpd]).status == 'needs_review'
    rpd['meta']['program_id'] = 'OTHER'
    assert check('R-14', [characteristic, rpd]).status == 'needs_review'
    rpd['meta'].pop('program_id')
    assert check('R-14', [characteristic, rpd]).status == 'needs_review'


@pytest.mark.parametrize('role', ['RPD', 'RPP'])
def test_direct_and_indirect_paths_are_checked_per_outcome(role):
    data = path_doc(role)
    data['learning_outcomes'].append({'local_id': 'L2'})
    data['indicator_outcome_links'] = [{'indicator_code': 'A.1', 'learning_outcome_id': 'L2'}]
    assert check('R-10', [data]).status == 'passed'
    data['relations'][1]['kind'] = 'other'
    result = check('R-10', [data])
    assert result.status == 'needs_review'
    assert [f.code for f in result.findings] == ['outcome_without_indicator']


def test_language_list_must_be_subset_of_program_languages():
    characteristic = doc('CHARACTERISTIC', program={'education_languages': ['русский', 'английский']})
    rpp = doc('RPP', education_languages=['русский', 'французский'])
    result = check('R-14', [characteristic, rpp])
    assert result.status == 'failed'
    assert [f.code for f in result.findings] == ['language_not_allowed']
    rpp['education_languages'] = ['русский', 'английский']
    assert check('R-14', [characteristic, rpp]).status == 'passed'


@pytest.mark.parametrize('codes,rows,expected', [
    (['A.01', 'A.1'], [1, 2], 'indicator_number_ambiguous'),
    (['A.1', 'A.2'], [1, 1], 'indicator_order_ambiguous'),
])
def test_ambiguous_indicator_number_or_order(codes, rows, expected):
    data = doc('CKMV', indicators=[
        {'code': code, 'competency_code': 'A',
         'source_ref': {'section': 'S', 'table': 1, 'row': row}}
        for code, row in zip(codes, rows)
    ])
    result = check('R-15', [data])
    assert result.status == 'needs_review'
    assert expected in {f.code for f in result.findings}
