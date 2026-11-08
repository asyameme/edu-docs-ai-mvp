"""Full R-01/R-07 regression checks without competency matrix."""
import copy
import unittest
from backend.worker.validation.validator import load_rules, run_validation


def document(role, **fields):
    return {'meta': {'doc_type': role.upper(), 'file_name': role + '.docx', 'program_id': 'P'}, **fields}


def only(rule_id):
    return {'rules': [copy.deepcopy(next(r for r in load_rules()['rules'] if r['id'] == rule_id))]}


class CompetencyRulesTests(unittest.TestCase):
    def test_01_validated_codes_and_matched_links(self):
        plan = document('curriculum', competencies=[{'code':'A'}],
            curriculum_items=[{'discipline_name':'D', 'semester':1}],
            relations=[{'src_entity':'curriculum_item','src_key':'D','dst_entity':'competency','dst_key':'A','kind':'формирует'}])
        rpd = document('rpd', competencies=[{'code':'A'}], disciplines=[{'name':'D','semesters':[1]}],
            relations=[{'src_entity':'discipline','src_key':'D','dst_entity':'competency','dst_key':'A','kind':'формирует'}])
        self.assertEqual(run_validation([plan,rpd],only('R-01')).results[0].status, 'passed')
        rpd['relations'][0]['dst_key'] = 'B'
        r = run_validation([plan,rpd],only('R-01')).results[0]
        self.assertEqual(r.status, 'failed')
        self.assertIn('unknown_competency_code',[f.code for f in r.findings])
        self.assertIn('competency_relation_mismatch',[f.code for f in r.findings])

    def test_01_missing_mandatory_competency_in_rpd(self):
        plan = document('curriculum', curriculum_items=[{'discipline_name':'D','semester':1}],
            relations=[{'src_entity':'curriculum_item','src_key':'D','dst_entity':'competency','dst_key':'A','kind':'формирует'}],
            competencies=[{'code':'A'}])
        rpd = document('rpd', disciplines=[{'name':'D','semesters':[1]}],
            competencies=[{'code':'A'}], relations=[{'src_entity':'discipline','src_key':'D','dst_entity':'competency','dst_key':'B','kind':'формирует'}])
        r=run_validation([plan,rpd],only('R-01')).results[0]
        self.assertEqual(r.status,'failed')
        self.assertEqual([f.evidence['only_in_curriculum'] for f in r.findings if f.code=='competency_relation_mismatch'],[['A']])

    def test_01_unmatched_plan_discipline_not_false_failure(self):
        plan=document('curriculum',curriculum_items=[{'discipline_name':'D','semester':1}],relations=[])
        rpd=document('rpd',disciplines=[{'name':'Other','semesters':[1]}],relations=[])
        self.assertEqual(run_validation([plan,rpd],only('R-01')).results[0].status,'skipped')

    def test_01_incomplete_catalog_is_review_not_error(self):
        ckmv=document('ckmv',competencies=[{'code':'A'}])
        rpd=document('rpd',competencies=[{'code':'B'}],relations=[{'src_entity':'discipline','src_key':'D','dst_entity':'competency','dst_key':'B'}])
        r=run_validation([ckmv,rpd],only('R-01')).results[0]
        self.assertEqual(r.status,'needs_review')
        self.assertEqual(r.findings[0].code,'competency_not_in_program_catalogue')

    def test_07_formatting_variants_allowed(self):
        ref=document('ckmv',competencies=[{'code':'A','formulation':'Способен  анализировать текст.'}])
        rpd=document('rpd',competencies=[{'code':'A','formulation':' способен анализировать ТЕКСТ '}])
        self.assertEqual(run_validation([ref,rpd],only('R-07')).results[0].status,'passed')

    def test_07_real_semantic_difference(self):
        ref=document('ckmv',competencies=[{'code':'A','formulation':'Способен анализировать устную речь'}])
        rpd=document('rpd',competencies=[{'code':'A','formulation':'Способен анализировать письменную речь'}])
        model=lambda payload, cfg: {'status':'changed','reason':'Объект анализа изменился'}
        r=run_validation([ref,rpd],only('R-07'),competency_evaluator=model).results[0]
        self.assertEqual(r.status,'failed')
        self.assertEqual(r.findings[0].code,'competency_formulation_mismatch')
        self.assertEqual(len(r.findings[0].evidence['source_refs']),2)

    def test_07_equivalent_paraphrase(self):
        ref=document('characteristic',competencies=[{'code':'A','formulation':'Использует методы анализа данных'}])
        rpd=document('rpd',competencies=[{'code':'A','formulation':'Применяет методы анализа данных'}])
        model=lambda payload, cfg: {'status':'equivalent','reason':'Смысл одинаков'}
        self.assertEqual(run_validation([ref,rpd],only('R-07'),competency_evaluator=model).results[0].status,'passed')

    def test_07_no_llm_fails_safe(self):
        ref=document('ckmv',competencies=[{'code':'A','formulation':'Устную речь'}])
        rpd=document('rpd',competencies=[{'code':'A','formulation':'Письменную речь'}])
        unavailable=lambda payload, cfg: (_ for _ in ()).throw(RuntimeError('No LLM'))
        r=run_validation([ref,rpd],only('R-07'),competency_evaluator=unavailable).results[0]
        self.assertEqual(r.status,'needs_review')
        self.assertEqual(r.findings[0].evidence['semantic_status'],'uncertain')

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
