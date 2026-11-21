"""R-07 deterministic cross-document grouping and user choice."""
from worker.validation.validator import load_rules, run_validation

RULES = load_rules()
RULE = {**RULES, 'rules': [next(r for r in RULES['rules'] if r['id'] == 'R-07')]}


def doc(kind, name, code='УК-1', formulation='Исходная формулировка', pid='p'):
    return {'meta': {'doc_type': kind, 'file_name': name, 'program_id': pid},
            'competencies': [{'code': code, 'formulation': formulation,
                              'source_ref': {'section': 'competencies'}}]}


def test_different_formulations_create_one_user_decision():
    result = run_validation([
        doc('rpd', 'a.json', formulation='Способен решать задачи.'),
        doc('rpp', 'b.json', formulation='Способен решать задачи на продвинутом уровне.'),
    ], RULE).results[0]
    assert result.status == 'needs_review'
    assert result.checks == 1
    assert len(result.findings) == 1
    finding = result.findings[0]
    assert finding.code == 'competency_formulation_review'
    assert finding.status == 'needs_review'
    assert finding.evidence['requires_user_confirmation'] is True
    assert [v['document'] for v in finding.evidence['variants']] == ['a.json', 'b.json']
    assert all(v['source_ref']['section'] == 'competencies' for v in finding.evidence['variants'])
    assert 'comparisons' not in finding.evidence


def test_ckmv_is_not_assumed_as_approved_reference():
    result = run_validation([
        doc('rpd', 'rpd.json', formulation='A'),
        doc('ckmv', 'ckmv.json', formulation='B'),
        doc('characteristic', 'characteristic.json', formulation='C'),
    ], RULE).results[0]
    assert result.checks == 3
    assert len(result.findings) == 1
    assert len(result.findings[0].evidence['variants']) == 3
    assert len(result.findings[0].evidence['source_refs']) == 3
    assert not any('reference_text' in f.evidence for f in result.findings)


def test_presentation_only_differences_do_not_require_review():
    result = run_validation([
        doc('rpd', 'a', formulation='  ТЕКСТ   компетенции. '),
        doc('rpp', 'b', formulation='текст компетенции'),
    ], RULE).results[0]
    assert result.status == 'passed'
    assert result.checks == 1


def test_yo_and_common_abbreviation_differences_do_not_require_review():
    result = run_validation([
        doc('rpd', 'a', formulation='Способен развёртывать решения, в том числе для облака'),
        doc('rpp', 'b', formulation='Способен развертывать решения, в т. ч. для облака'),
    ], RULE).results[0]
    assert result.status == 'passed'
    assert result.findings == []


def test_different_wording_requires_choice_without_semantic_classification():
    result = run_validation([
        doc('rpd', 'a', formulation='Умеет работать с данными.'),
        doc('rpp', 'b', formulation='Способен работать с данными.'),
    ], RULE).results[0]
    assert result.status == 'needs_review'
    assert result.findings[0].code == 'competency_formulation_review'
    assert result.findings[0].evidence['requires_user_confirmation'] is True


def test_different_codes_are_not_matched_by_r07():
    result = run_validation([
        doc('rpd', 'a', code='УНК-03'),
        doc('rpp', 'b', code='ПКП-УНК-03'),
    ], RULE).results[0]
    assert result.checks == 0
    assert result.status == 'skipped'


def test_missing_formulation_and_program_boundaries():
    result = run_validation([
        doc('rpd', 'a', formulation=''),
        doc('rpp', 'b', formulation='Непустая'),
        doc('rpd', 'other', pid='another'),
    ], RULE).results[0]
    assert result.checks == 1
    assert result.findings[0].code == 'competency_formulation_missing'
    assert result.findings[0].status == 'needs_review'


def test_three_documents_create_one_decision_covering_all_sources():
    files = [
        doc('rpd', 'one.json', formulation='Способен анализировать данные'),
        doc('rpp', 'two.json', formulation='Способен анализировать большие данные'),
        doc('matrix', 'three.json', formulation='Способен анализировать текстовые данные'),
    ]
    result = run_validation(files, RULE).results[0]
    assert result.checks == 3
    assert len(result.findings) == 1
    assert len(result.findings[0].evidence['variants']) == 3
    assert {ref['file'] for ref in result.findings[0].evidence['source_refs']} == {
        'one.json', 'two.json', 'three.json'
    }
