import unittest
from backend.worker.validation.validator import load_rules, run_validation

def doc(role,**kw): return {'meta':{'doc_type':role,'program_id':'p'},**kw}
def check(rule,docs,status):
 import copy
 c=copy.deepcopy(load_rules()); c['rules']=[r for r in c['rules'] if r['id']==rule]; c['rules'][0]['status']='ready'; r=run_validation(docs,c,phase=c['rules'][0]['phases'][0]).results[0]; assert r.status==status,(rule,r); print(rule,status)

class WorkloadReviewTests(unittest.TestCase):
    def test_workload_and_relation_cases(self):
        a=doc('rpd',disciplines=[{'name':'A','semesters':[1],'hours_total':36,'credit_units':1,'hours_by_kind':{'лекции':36}}]); b=doc('curriculum',curriculum_items=[{'discipline_name':'A','semester':1,'hours_total':36,'credit_units':1}]);
        for rule in ['R-02','R-05']:
         check(rule,[a,b],'passed'); check(rule,[a],'needs_review'); check(rule,[doc('rpd',disciplines=[{'name':'A','semesters':[1,2],'hours_total':72,'credit_units':2}]),b],'needs_review'); check(rule,[a,doc('curriculum',curriculum_items=[{'discipline_name':'A','semester':1,'hours_total':72,'credit_units':2}])],'failed')
        check('R-03',[a],'passed'); check('R-03',[doc('rpd',disciplines=[{'hours_total':36,'hours_by_kind':{'лекции':36,'unknown':1}}])],'needs_review')
        check('R-03-RPP',[doc('rpp',disciplines=[{},{}])],'needs_review'); check('R-03-RPP',[doc('rpp',disciplines=[{'semesters':[1],'hours_total':36,'credit_units':1}],semester_workload=[{'semester':1,'hours_total':36,'credit_units':1}])],'passed')
        r={'src_entity':'discipline','src_key':'A','dst_entity':'competency','dst_key':'B','kind':'формирует'}; check('R-06',[doc('rpd',relations=[r,r])],'needs_review'); check('R-06',[doc('rpd',relations=[dict(r,semester=1),dict(r,semester=2)])],'passed')
