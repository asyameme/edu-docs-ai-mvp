from worker.validation.validator import load_rules, run_validation


def check(rule_id, documents):
    config = load_rules()
    rule = next(r for r in config["rules"] if r["id"] == rule_id)
    rule["status"] = "ready"
    config["rules"] = [rule]
    return run_validation(
        documents, config, phase=rule["phases"][0]
    ).results[0]


def curriculum():
    items = []
    relations = []

    for semester, row in [(1, 6), (2, 58)]:
        ref = {
            "file": "UP.docx",
            "table": 4,
            "row": row,
            "section": f"Semester {semester}",
        }
        items.append({
            "discipline_name": "Algebra",
            "semester": semester,
            "source_ref": {**ref, "col": 4},
        })
        relations.append({
            "src_entity": "curriculum_item",
            "src_key": "Algebra",
            "dst_entity": "competency",
            "dst_key": "A",
            "kind": "формирует",
            "source_ref": {**ref, "col": 3},
        })

    return {
        "meta": {"doc_type": "curriculum"},
        "curriculum_items": items,
        "relations": relations,
    }


def test_relations_in_different_semesters_are_not_duplicates():
    result = check("R-06", [curriculum()])
    assert result.status == "passed"
    assert not result.findings


def test_relations_in_same_context_require_review():
    doc = curriculum()
    doc["relations"].append(dict(doc["relations"][0]))

    result = check("R-06", [doc])
    assert result.status == "needs_review"
    assert result.findings


def test_duplicate_competency_in_different_ckmv_levels():
    doc = {
        "meta": {"doc_type": "ckmv"},
        "competencies": [
            {
                "code": "A",
                "formulation": "Same formulation",
                "source_ref": {
                    "section": "Профессиональные перекрестные компетенции базового уровня",
                    "row": 105,
                },
            },
            {
                "code": "A",
                "formulation": "Same formulation",
                "source_ref": {
                    "section": "Профессиональные перекрестные компетенции топ-уровня",
                    "row": 221,
                },
            },
        ],
    }

    result = check("R-06", [doc])
    assert result.status == "passed"


def test_duplicate_competency_in_same_level_fails():
    doc = {
        "meta": {"doc_type": "ckmv"},
        "competencies": [
            {
                "code": "A",
                "formulation": "Same formulation",
                "source_ref": {
                    "section": "Профессиональные перекрестные компетенции базового уровня",
                    "row": row,
                },
            }
            for row in [105, 115]
        ],
    }

    result = check("R-06", [doc])
    assert result.status == "failed"


def test_workload_candidate_requires_manual_review():
    documents = [
        {
            "meta": {"doc_type": "rpd", "program_id": "P"},
            "disciplines": [{
                "name": "Математика",
                "semesters": [1],
                "hours_total": 108,
                "credit_units": 3,
            }],
        },
        {
            "meta": {"doc_type": "curriculum", "program_id": "P"},
            "curriculum_items": [{
                "discipline_name": "Математика (осн курс), осн тр",
                "semester": 1,
                "hours_total": 108,
                "credit_units": 3,
            }],
        },
    ]

    result = check("R-02", documents)

    assert result.status == "needs_review"
    assert any(
        f.code == "workload_match_candidate"
        for f in result.findings
    )
