from worker.validation.validator import _references, Document


def make_rule():
    return {
        "id": "R-08-local-reference",
        "type": "reference_exists",
        "scope": {"document_types": ["rpd", "rpp", "ckmv"]},
        "inputs": {
            "source_records_path": "$.indicators[*]",
            "source_value_field": "competency_code",
            "target_records_path": "$.competencies[*]",
            "target_key_field": "code",
        },
    }


def make_doc(code):
    return Document(
        data={
            "meta": {"doc_type": "rpd"},
            "competencies": [{"code": "COMP-1"}],
            "indicators": [{"competency_code": code}],
        },
        label="test_rpd.json",
    )


def test_r08_valid_reference():
    result = _references(make_rule(), [make_doc("COMP-1")])
    assert not result.findings


def test_r08_dangling_reference():
    result = _references(make_rule(), [make_doc("COMP-2")])
    assert len(result.findings) == 1
    assert result.findings[0].code == "dangling_reference"


def test_r08_fixed_reference():
    before = _references(make_rule(), [make_doc("COMP-2")])
    after = _references(make_rule(), [make_doc("COMP-1")])

    assert len(before.findings) == 1
    assert not after.findings
