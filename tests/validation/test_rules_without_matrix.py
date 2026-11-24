"""Регрессии структурных правил, не зависящих от матрицы компетенций."""
import copy
import unittest
from unittest.mock import patch

from backend.worker.validation.validator import Document, load_rules, run_validation


def doc(kind, **fields):
    return {"meta": {"doc_type": kind.upper(), "program_id": "P", "file_name": kind + ".docx"}, **fields}


def rule(rule_id):
    config = load_rules()
    selected = copy.deepcopy(next(r for r in config["rules"] if r["id"] == rule_id))
    # Черновой обработчик включается только в изолированном тесте.
    selected["status"] = "ready"
    return {**config, "rules": [selected]}


def run_rule(documents, config, **scope):
    selected = config["rules"][0]
    rule_codes = [selected["id"]] if selected.get("run_mode") == "manual" else None
    return run_validation(
        documents,
        config,
        phase=selected["phases"][0],
        rule_codes=rule_codes,
        **scope,
    ).results[0]


class ValidationWithoutMatrixTests(unittest.TestCase):
    def test_hours_across_documents(self):
        curriculum = doc("curriculum", curriculum_items=[{"discipline_name": "D", "semester": 1, "hours_total": 216, "source_ref": {"file": "u"}}])
        rpd = doc("rpd", disciplines=[{"name": "D", "semesters": [1], "hours_total": 206, "source_ref": {"file": "d"}}])
        result = run_rule([curriculum, rpd], rule("R-02"))
        self.assertEqual(result.status,"failed")
        self.assertEqual(result.findings[0].code,"curriculum_hours_mismatch")
        rpd["disciplines"][0]["hours_total"] = 216
        self.assertEqual(run_rule([curriculum, rpd], rule("R-02")).status,"passed")

    def test_credit_hours_and_semester_totals(self):
        practice = doc("rpp", disciplines=[{"name":"P", "semesters":[2,3], "hours_total": 648, "credit_units":18}],semester_workload=[{"semester":2,"hours_total":108,"credit_units":3},{"semester":3,"hours_total":540,"credit_units":15}])
        self.assertEqual(run_rule([practice],rule("R-04")).status,"passed")
        self.assertEqual(run_rule([practice],rule("R-03-RPP")).status,"passed")
        practice["semester_workload"][1]["hours_total"] = 500
        self.assertEqual(run_rule([practice],rule("R-04")).status,"failed")
        self.assertEqual(run_rule([practice],rule("R-03-RPP")).status,"failed")

    def test_incomplete_curriculum_does_not_fail_coverage(self):
        characteristic = doc("characteristic",competencies=[{"code":"U"},{"code":"V"}])
        plan = doc("curriculum",relations=[])
        incomplete = rule("R-09")
        incomplete["rules"][0]["inputs"]["curriculum_is_complete"] = False
        self.assertEqual(
            run_rule([characteristic, plan], incomplete, competency_code="U").status, "skipped"
        )
        enabled = rule("R-09")
        enabled["rules"][0]["inputs"]["curriculum_is_complete"] = True
        result = run_rule([characteristic, plan], enabled, competency_code="U")
        self.assertEqual(result.status,"failed")
        self.assertEqual(len(result.findings), 1)
        self.assertEqual(result.findings[0].code,"competency_not_covered")
        self.assertIn("'U'", result.findings[0].message)

    def test_coverage_only_requests_the_selected_competency(self):
        characteristic = doc("characteristic", competencies=[
            {"code": "U", "formulation": "Выбранная компетенция"},
            {"code": "V", "formulation": "Другая компетенция"},
        ])
        plan = doc("curriculum", curriculum_items=[{"discipline_name": "D", "semester": 1}],
                   relations=[
                       {"src_entity": "curriculum_item", "src_key": "D",
                        "dst_entity": "competency", "dst_key": code, "kind": "формирует"}
                       for code in ("U", "V")
                   ])
        rpd = doc("rpd", disciplines=[{"name": "D", "semesters": [1]}],
                  course_content=[{"topic": "Общая тема"}])
        seen = []

        def evaluate(payload, _config):
            groups = payload["competency_groups"]
            seen.extend(group["competency_code"] for group in groups)
            return {"results": [
                {"group_id": group["group_id"], "status": "supported",
                 "reason": "Содержание подтверждает покрытие", "evidence_ids": [group["evidence"][0]["id"]]}
                for group in groups
            ]}

        result = run_rule([characteristic, plan, rpd], rule("R-09"),
                          competency_code="U", semantic_evaluator=evaluate)

        self.assertEqual(seen, ["U"])
        self.assertEqual(result.status, "passed")

    def test_alignment_only_reads_the_selected_rpd(self):
        documents = [Document(doc("rpd"), label, label) for label in ("first", "second")]
        with patch("backend.worker.validation.validator.evaluate_rpd_alignment",
                   return_value=(1, [])) as evaluate:
            result = run_rule(documents, rule("R-11"), document_ids=["second"])
        self.assertEqual(result.checks, 1)
        evaluate.assert_called_once()
        self.assertEqual(evaluate.call_args.kwargs["document_label"], "second")

    def test_outcome_links(self):
        ck=doc("ckmv", learning_outcomes=[{"local_id":"L"}], indicators=[{"code":"I"}],indicator_outcome_links=[{"indicator_code":"MISSING", "learning_outcome_id":"L"}])
        result=run_rule([ck],rule("R-10"))
        self.assertEqual(result.status,"failed")
        ck["indicator_outcome_links"][0]["indicator_code"]="I"
        self.assertEqual(run_rule([ck],rule("R-10")).status,"passed")

    def test_indicator_hierarchy(self):
        ck=doc("ckmv",indicators=[{"code":"B.1", "competency_code":"A"}])
        self.assertEqual(run_rule([ck],rule("R-15")).status,"failed")
        ck["indicators"][0]["code"]="A.1"
        self.assertEqual(run_rule([ck],rule("R-15")).status,"passed")

    def test_missing_structure_and_language_fields_are_skipped(self):
        self.assertEqual(run_rule([doc("ckmv")],rule("R-13")).status,"skipped")
        self.assertEqual(run_rule([doc("characteristic"),doc("rpd")],rule("R-14")).status,"skipped")

    def test_hours_sum_includes_seminars_and_lab_hours(self):
        rpd = doc("rpd", disciplines=[{
            "hours_total": 108,
            "hours_by_kind": {"лекции": 40, "семинары": 32, "лабораторные работы": 36},
        }])
        self.assertEqual(run_rule([rpd], rule("R-03")).status, "passed")
        rpd["disciplines"][0]["hours_total"] = 109
        result = run_rule([rpd], rule("R-03"))
        self.assertEqual(result.findings[0].code, "hours_sum_mismatch")

    def test_title_credit_mismatch_reports_both_sources(self):
        title = {"file": "rpd.docx", "section": "титул", "paragraph_index": 6}
        table = {"file": "rpd.docx", "section": "2.1.1", "table": 2, "row": 7}
        rpd = doc(
            "rpd", title_credit_units=6, title_credit_units_source_ref=title,
            disciplines=[{"credit_units": 3, "source_ref": table}],
        )
        result = run_rule([rpd], rule("R-05-title"))
        self.assertEqual(result.findings[0].code, "title_credit_units_mismatch")
        self.assertEqual(result.findings[0].evidence["source_refs"], [title, table])

    def test_partial_semester_workload_requires_review(self):
        rpp = doc(
            "rpp",
            disciplines=[{"semesters": [2, 3], "hours_total": 216, "credit_units": 6}],
            semester_workload=[{"semester": 2, "hours_total": 108, "credit_units": 3}],
        )
        result = run_rule([rpp], rule("R-03-RPP"))
        self.assertEqual(result.status, "needs_review")
        self.assertEqual(result.findings[0].code, "incomplete_semester_workload")


if __name__ == "__main__":
    unittest.main()
