"""Регрессии структурных правил, не зависящих от матрицы компетенций."""
import copy
import unittest

from backend.worker.validation.validator import load_rules, run_validation


def doc(kind, **fields):
    return {"meta": {"doc_type": kind.upper(), "program_id": "P", "file_name": kind + ".docx"}, **fields}


def rule(rule_id):
    rules = load_rules()["rules"]
    return {"rules": [copy.deepcopy(next(r for r in rules if r["id"] == rule_id))]}


class ValidationWithoutMatrixTests(unittest.TestCase):
    def test_hours_across_documents(self):
        curriculum = doc("curriculum", curriculum_items=[{"discipline_name": "D", "semester": 1, "hours_total": 216, "source_ref": {"file": "u"}}])
        rpd = doc("rpd", disciplines=[{"name": "D", "semesters": [1], "hours_total": 206, "source_ref": {"file": "d"}}])
        result = run_validation([curriculum, rpd], rule("R-02")).results[0]
        self.assertEqual(result.status,"failed")
        self.assertEqual(result.findings[0].code,"curriculum_hours_mismatch")
        rpd["disciplines"][0]["hours_total"] = 216
        self.assertEqual(run_validation([curriculum, rpd], rule("R-02")).results[0].status,"passed")

    def test_credit_hours_and_semester_totals(self):
        practice = doc("rpp", disciplines=[{"name":"P", "hours_total": 648, "credit_units":18}],semester_workload=[{"semester":2,"hours_total":108,"credit_units":3},{"semester":3,"hours_total":540,"credit_units":15}])
        self.assertEqual(run_validation([practice],rule("R-04")).results[0].status,"passed")
        self.assertEqual(run_validation([practice],rule("R-03-RPP")).results[0].status,"passed")
        practice["semester_workload"][1]["hours_total"] = 500
        self.assertEqual(run_validation([practice],rule("R-04")).results[0].status,"failed")
        self.assertEqual(run_validation([practice],rule("R-03-RPP")).results[0].status,"failed")

    def test_incomplete_curriculum_does_not_fail_coverage(self):
        characteristic = doc("characteristic",competencies=[{"code":"U"}])
        plan = doc("curriculum",relations=[])
        self.assertEqual(run_validation([characteristic,plan],rule("R-09")).results[0].status,"skipped")
        enabled=rule("R-09")
        enabled["rules"][0]["inputs"]["curriculum_is_complete"] = True
        result=run_validation([characteristic,plan],enabled).results[0]
        self.assertEqual(result.status,"failed")
        self.assertEqual(result.findings[0].code,"competency_not_covered")

    def test_outcome_links(self):
        ck=doc("ckmv", learning_outcomes=[{"local_id":"L"}], indicators=[{"code":"I"}],indicator_outcome_links=[{"indicator_code":"MISSING", "learning_outcome_id":"L"}])
        result=run_validation([ck],rule("R-10")).results[0]
        self.assertEqual(result.status,"failed")
        ck["indicator_outcome_links"][0]["indicator_code"]="I"
        self.assertEqual(run_validation([ck],rule("R-10")).results[0].status,"passed")

    def test_indicator_hierarchy(self):
        ck=doc("ckmv",indicators=[{"code":"B.1", "competency_code":"A"}])
        self.assertEqual(run_validation([ck],rule("R-15")).results[0].status,"failed")
        ck["indicators"][0]["code"]="A.1"
        self.assertEqual(run_validation([ck],rule("R-15")).results[0].status,"passed")

    def test_missing_structure_and_language_fields_are_skipped(self):
        self.assertEqual(run_validation([doc("ckmv")],rule("R-13")).results[0].status,"skipped")
        self.assertEqual(run_validation([doc("characteristic"),doc("rpd")],rule("R-14")).results[0].status,"skipped")


if __name__ == "__main__":
    unittest.main()
