"""Full R-01/R-07 regression checks without competency matrix."""
import copy
import unittest
from backend.worker.validation.validator import load_rules, run_validation


def document(role, **fields):
    return {'meta': {'doc_type': role.upper(), 'file_name': role + '.docx', 'program_id': 'P'}, **fields}


def only(rule_id):
    config = load_rules()
    return {**config, 'rules': [copy.deepcopy(next(r for r in config['rules'] if r['id'] == rule_id))]}


def graph_r01(documents):
    return run_validation(documents, only('R-01-graph'), phase='graph_validation').results[0]


class CompetencyRulesTests(unittest.TestCase):
    def test_01_validated_codes_and_matched_links(self):
        plan = document('curriculum', competencies=[{'code':'A'}],
            curriculum_items=[{'discipline_name':'D', 'semester':1}],
            relations=[{'src_entity':'curriculum_item','src_key':'D','dst_entity':'competency','dst_key':'A','kind':'формирует'}])
        rpd = document('rpd', competencies=[{'code':'A'}], disciplines=[{'name':'D','semesters':[1]}],
            relations=[{'src_entity':'discipline','src_key':'D','dst_entity':'competency','dst_key':'A','kind':'формирует'}])
        self.assertEqual(graph_r01([plan,rpd]).status, 'passed')
        rpd['relations'][0]['dst_key'] = 'B'
        r = graph_r01([plan,rpd])
        self.assertEqual(r.status, 'failed')
        self.assertIn('unknown_competency_code',[f.code for f in r.findings])
        self.assertIn('competency_relation_mismatch',[f.code for f in r.findings])

    def test_01_missing_mandatory_competency_in_rpd(self):
        plan = document('curriculum', curriculum_items=[{'discipline_name':'D','semester':1}],
            relations=[{'src_entity':'curriculum_item','src_key':'D','dst_entity':'competency','dst_key':'A','kind':'формирует'}],
            competencies=[{'code':'A'}])
        rpd = document('rpd', disciplines=[{'name':'D','semesters':[1]}],
            competencies=[{'code':'A'}], relations=[{'src_entity':'discipline','src_key':'D','dst_entity':'competency','dst_key':'B','kind':'формирует'}])
        r=graph_r01([plan,rpd])
        self.assertEqual(r.status,'failed')
        self.assertEqual([f.evidence['only_in_curriculum'] for f in r.findings if f.code=='competency_relation_mismatch'],[['A']])

    def test_01_unmatched_plan_discipline_not_false_failure(self):
        plan=document('curriculum',curriculum_items=[{'discipline_name':'D','semester':1}],relations=[])
        rpd=document('rpd',disciplines=[{'name':'Other','semesters':[1]}],relations=[])
        self.assertEqual(graph_r01([plan,rpd]).status,'skipped')

    def test_01_incomplete_catalog_is_review_not_error(self):
        ckmv=document('ckmv',competencies=[{'code':'A'}])
        rpd=document('rpd',competencies=[{'code':'B'}],relations=[{'src_entity':'discipline','src_key':'D','dst_entity':'competency','dst_key':'B'}])
        r=graph_r01([ckmv,rpd])
        self.assertEqual(r.status,'needs_review')
        self.assertEqual(r.findings[0].code,'competency_not_in_program_catalogue')

    def test_01_missing_catalogue_code_is_one_finding_with_every_source(self):
        documents = [document('ckmv', competencies=[{'code': 'A'}])]
        for index in range(3):
            parsed = document('rpd', competencies=[{
                'code': 'B', 'source_ref': {'section': 'competencies'},
            }], relations=[{
                'src_entity': 'discipline', 'src_key': 'D',
                'dst_entity': 'competency', 'dst_key': 'B',
                'source_ref': {'row': row},
            } for row in range(100)])
            parsed['meta']['file_name'] = f'rpd-{index}.docx'
            documents.append(parsed)
        original = copy.deepcopy(documents)
        result = graph_r01(documents)
        self.assertEqual(result.status, 'needs_review')
        self.assertEqual(len(result.findings), 1)
        finding = result.findings[0]
        self.assertEqual(finding.evidence['occurrence_count'], 300)
        self.assertEqual(len(finding.evidence['source_refs']), 303)
        self.assertEqual({ref['file'] for ref in finding.evidence['source_refs']},
                         {'rpd-0.docx', 'rpd-1.docx', 'rpd-2.docx'})
        self.assertEqual(documents, original)

    def test_01_unknown_code_remains_failed_when_repetitions_are_grouped(self):
        parsed = document('rpd', competencies=[{'code': 'A'}], relations=[{
            'src_entity': 'discipline', 'src_key': 'D',
            'dst_entity': 'competency', 'dst_key': 'missing',
            'source_ref': {'row': index},
        } for index in range(10)])
        result = graph_r01([parsed])
        self.assertEqual(result.status, 'failed')
        self.assertEqual(len(result.findings), 1)
        self.assertEqual(result.findings[0].code, 'unknown_competency_code')
        self.assertEqual(result.findings[0].evidence['occurrence_count'], 10)

    def test_07_formatting_variants_allowed(self):
        ref=document('ckmv',competencies=[{'code':'A','formulation':'Способен  анализировать текст.'}])
        rpd=document('rpd',competencies=[{'code':'A','formulation':' способен анализировать ТЕКСТ '}])
        self.assertEqual(run_validation([ref,rpd],only('R-07')).results[0].status,'passed')

    def test_07_different_wording_requires_choice(self):
        ref=document('ckmv',competencies=[{'code':'A','formulation':'Способен анализировать устную речь'}])
        rpd=document('rpd',competencies=[{'code':'A','formulation':'Способен анализировать письменную речь'}])
        r=run_validation([ref,rpd],only('R-07')).results[0]
        self.assertEqual(r.status,'needs_review')
        self.assertEqual(r.findings[0].code,'competency_formulation_review')
        self.assertEqual(len(r.findings[0].evidence['source_refs']),2)

    def test_07_changed_wording_requires_manual_choice(self):
        ref=document('characteristic',competencies=[{'code':'A','formulation':'Использует методы анализа данных'}])
        rpd=document('rpd',competencies=[{'code':'A','formulation':'Применяет методы анализа данных'}])
        self.assertEqual(run_validation([ref,rpd],only('R-07')).results[0].status,'needs_review')

    def test_07_different_codes_not_equated(self):
        ref=document('ckmv',competencies=[{'code':'A','formulation':'Same'}])
        rpd=document('rpd',competencies=[{'code':'B','formulation':'Same'}])
        self.assertEqual(run_validation([ref,rpd],only('R-07')).results[0].status,'skipped')

    def test_07_different_programs_not_compared(self):
        ref=document('ckmv',competencies=[{'code':'A','formulation':'Different'}])
        rpd=document('rpd',competencies=[{'code':'A','formulation':'Another'}]);rpd['meta']['program_id']='P2'
        self.assertEqual(run_validation([ref,rpd],only('R-07')).results[0].status,'skipped')


if __name__=='__main__':
    unittest.main()
