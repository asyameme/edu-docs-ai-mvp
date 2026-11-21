import json
from uuid import uuid4

import pytest
from document_cases import payload

import app.validation_bridge as validation_bridge
from app.domain import SnapshotV2
from app.validation_bridge import (
    _ValidationBudget,
    project_snapshot_documents,
    run_local_check,
    validation_ruleset,
)
from worker.validation.validator import Document, _source_ref, load_rules


def test_local_check_maps_cross_document_mismatch_to_correct_source_ids():
    curriculum_id, rpd_id = uuid4(), uuid4()
    curriculum_ref, rpd_ref = uuid4(), uuid4()
    snapshot = SnapshotV2.model_validate(
        {
            "document_ids": [curriculum_id, rpd_id],
            "entities": [],
            "relations": [],
            "source_links": [],
            "source_refs": [
                {"id": curriculum_ref, "document_id": curriculum_id, "locator": '{"row":1}'},
                {"id": rpd_ref, "document_id": rpd_id, "locator": '{"row":2}'},
            ],
        }
    )
    docs = [
        {
            "document_id": str(curriculum_id),
            "parsed": {
                "meta": {"doc_type": "CURRICULUM", "program_id": "P"},
                "curriculum_items": [
                    {
                        "discipline_name": "Алгебра",
                        "semester": 1,
                        "hours_total": 144,
                        "source_ref": {"row": 1},
                    }
                ],
            },
        },
        {
            "document_id": str(rpd_id),
            "parsed": {
                "meta": {"doc_type": "RPD", "program_id": "P"},
                "disciplines": [
                    {
                        "name": "Алгебра",
                        "semesters": [1],
                        "hours_total": 108,
                        "source_ref": {"row": 2},
                    }
                ],
            },
        },
    ]
    config = {
        "schema_version": 2,
        "phases": {
            "document_reconciliation": {"input": "parsed_documents"},
            "graph_validation": {"input": "graph_projection"},
        },
        "rules": [
            {
                "id": "R-02",
                "name": "Hours",
                "type": "hours_against_curriculum",
                "status": "ready",
                "phases": ["document_reconciliation", "graph_validation"],
            }
        ],
    }
    ruleset = {
        "id": "test",
        "revision": "r1",
        "config": config,
        "required_rules": ["R-02"],
        "required_document_types": ["curriculum", "rpd"],
    }
    result = run_local_check(docs, snapshot, uuid4(), config=config, ruleset=ruleset)
    assert result["coverage"][0]["status"] == "failed"
    assert result["coverage"][0]["checks"] == 1
    issue = result["issues"][0]
    assert issue["source_ref_ids"] == [str(curriculum_ref), str(rpd_ref)]
    assert issue["severity"] == "warning"


def test_local_check_maps_filename_evidence_to_document_source_ref(monkeypatch):
    document_id, source_ref_id = uuid4(), uuid4()
    locator = {
        "file": "RPD.docx",
        "row": 2,
        "table": 1,
        "section": "Competencies",
        "col": 1,
        "cell": None,
        "sheet_name": None,
        "paragraph_index": 7,
    }
    snapshot = SnapshotV2.model_validate(
        {
            "document_ids": [document_id],
            "entities": [],
            "relations": [],
            "source_links": [],
            "source_refs": [
                {
                    "id": source_ref_id,
                    "document_id": document_id,
                    "locator": json.dumps(locator, ensure_ascii=False),
                }
            ],
        }
    )
    documents = [
        {
            "document_id": str(document_id),
            "parsed": {
                "meta": {"doc_type": "RPD", "file_name": "RPD.docx"},
                "disciplines": [],
            },
        }
    ]
    config = {
        "schema_version": 2,
        "phases": {
            "document_reconciliation": {"input": "parsed_documents"},
            "graph_validation": {"input": "graph_projection"},
        },
        "rules": [
            {
                "id": "R-07",
                "name": "Semantics",
                "type": "example",
                "status": "ready_with_semantic_fallback",
                "phases": ["document_reconciliation", "graph_validation"],
            }
        ],
    }
    monkeypatch.setattr(
        validation_bridge,
        "run_validation",
        lambda *_, **__: type(
            "Report",
            (),
            {
                "to_dict": lambda self: {
                    "results": [
                        {
                            "rule_id": "R-07",
                            "status": "needs_review",
                            "checks": 1,
                            "findings": [
                                {
                                    "code": "semantic_uncertain",
                                    "status": "needs_review",
                                    "message": "Manual review required",
                                    "evidence": {"source_refs": [locator]},
                                }
                            ],
                        }
                    ]
                }
            },
        )(),
    )
    ruleset = {
        "id": "test",
        "revision": "r1",
        "config": config,
        "required_rules": ["R-07"],
        "required_document_types": ["rpd"],
    }

    result = run_local_check(documents, snapshot, uuid4(), config=config, ruleset=ruleset)

    assert result["issues"][0]["source_ref_ids"] == [str(source_ref_id)]


def test_local_check_uses_document_ids_for_duplicate_filenames(monkeypatch):
    import json
    from types import SimpleNamespace

    document_ids, source_ref_ids = [uuid4(), uuid4()], [uuid4(), uuid4()]
    locator = {"file": "duplicate.docx", "row": 2}
    snapshot = SnapshotV2.model_validate(
        {
            "document_ids": document_ids,
            "entities": [],
            "relations": [],
            "source_links": [],
            "source_refs": [
                {
                    "id": ref_id,
                    "document_id": document_id,
                    "locator": json.dumps(locator),
                }
                for document_id, ref_id in zip(document_ids, source_ref_ids, strict=True)
            ],
        }
    )
    documents = [
        {
            "document_id": str(document_id),
            "parsed": {
                "meta": {"doc_type": "RPD", "file_name": "duplicate.docx"},
                "disciplines": [],
            },
        }
        for document_id in document_ids
    ]
    config = {
        "schema_version": 2,
        "phases": {
            "document_reconciliation": {"input": "parsed_documents"},
            "graph_validation": {"input": "graph_projection"},
        },
        "rules": [
            {
                "id": "R-07",
                "name": "Semantics",
                "type": "example",
                "status": "ready_with_semantic_fallback",
                "phases": ["document_reconciliation", "graph_validation"],
            }
        ],
    }
    report = {
        "results": [
            {
                "rule_id": "R-07",
                "status": "needs_review",
                "checks": 1,
                "findings": [
                    {
                        "code": "semantic_uncertain",
                        "status": "needs_review",
                        "message": "Manual review required",
                        "evidence": {"source_refs": [
                            {**locator, "document_id": str(document_ids[0])},
                            {**locator, "document_id": str(document_ids[1])},
                        ]},
                    }
                ],
            }
        ]
    }
    monkeypatch.setattr(
        validation_bridge,
        "run_validation",
        lambda *_, **__: SimpleNamespace(to_dict=lambda: report),
    )
    ruleset = {
        "id": "test",
        "revision": "r1",
        "config": config,
        "required_rules": ["R-07"],
        "required_document_types": ["rpd"],
    }

    result = run_local_check(documents, snapshot, uuid4(), config=config, ruleset=ruleset)

    assert result["issues"][0]["source_ref_ids"] == [str(source_ref_ids[0]), str(source_ref_ids[1])]


def test_real_findings_keep_distinct_sources_for_same_named_documents():
    document_ids, ref_ids = [uuid4(), uuid4()], [uuid4(), uuid4()]
    locator = {"file": "same.docx", "row": 1}
    snapshot = SnapshotV2.model_validate({
        "document_ids": document_ids,
        "source_refs": [
            {"id": ref_id, "document_id": document_id, "locator": json.dumps(locator)}
            for document_id, ref_id in zip(document_ids, ref_ids, strict=True)
        ],
    })
    documents = [
        {
            "document_id": str(document_id),
            "parsed": {
                "meta": {"doc_type": "RPD", "file_name": "same.docx", "program_id": "P"},
                "relations": [{
                    "src_entity": "discipline",
                    "src_key": "A",
                    "dst_entity": "competency",
                    "dst_key": "missing",
                    "kind": "формирует",
                    "source_ref": locator,
                }],
            },
        }
        for document_id in document_ids
    ]

    result = run_local_check(documents, snapshot, uuid4(), config=load_rules())

    actual = [
        issue["source_ref_ids"]
        for issue in result["issues"]
        if issue["rule_code"] == "R-01-graph"
    ]
    assert actual == [[str(ref_id)] for ref_id in ref_ids]


def test_local_check_does_not_guess_between_duplicate_locators_in_one_document(monkeypatch):
    from types import SimpleNamespace

    document_id, source_ref_ids = uuid4(), [uuid4(), uuid4()]
    locator = {"file": "same.docx", "row": 2}
    snapshot = SnapshotV2.model_validate({
        "document_ids": [document_id],
        "source_refs": [
            {
                "id": ref_id,
                "document_id": document_id,
                "locator": json.dumps(locator),
            }
            for ref_id in source_ref_ids
        ],
    })
    document = {
        "document_id": str(document_id),
        "parsed": {"meta": {"doc_type": "RPD", "file_name": "same.docx"}},
    }
    report = {"results": [{
        "rule_id": "R-07",
        "status": "needs_review",
        "checks": 1,
        "findings": [{
            "code": "source_ambiguous",
            "status": "needs_review",
            "message": "Manual review required",
            "evidence": {"source_refs": [{**locator, "document_id": str(document_id)}]},
        }],
    }]}
    monkeypatch.setattr(
        validation_bridge,
        "run_validation",
        lambda *_, **__: SimpleNamespace(to_dict=lambda: report),
    )
    config = {
        "schema_version": 2,
        "phases": {
            "document_reconciliation": {"input": "parsed_documents"},
            "graph_validation": {"input": "graph_projection"},
        },
        "rules": [{
        "id": "R-07", "name": "Semantics", "type": "example", "status": "ready",
        "phases": ["document_reconciliation", "graph_validation"]
    }]}
    ruleset = {
        "id": "test", "revision": "r1", "config": config,
        "required_rules": ["R-07"], "required_document_types": ["rpd"],
    }

    result = run_local_check([document], snapshot, uuid4(), config=config, ruleset=ruleset)

    issue = result["issues"][0]
    assert issue["source_ref_ids"] == []
    assert issue["evidence"]["source_mapping_limitations"] == [
        "Источник same.docx неоднозначен по координатам"
    ]


def test_recheck_preserves_unchanged_unknown_numeric_field():
    document_id, entity_id, ref_id = uuid4(), uuid4(), uuid4()
    document = {"document_id": str(document_id), "parsed": {"disciplines": [{
        "credit_units": None, "source_ref": {"row": 2},
    }]}}
    snapshot = SnapshotV2.model_validate({
        "document_ids": [document_id], "entities": [{
            "id": entity_id, "kind": "discipline", "attributes": {"credit_units": None},
            "source_ref_ids": [ref_id],
        }], "source_refs": [{
            "id": ref_id, "document_id": document_id, "locator": '{"row":2}',
        }], "source_links": [{
            "id": uuid4(), "target_kind": "entity", "target_id": entity_id,
            "source_ref_id": ref_id, "field_path": "/attributes/credit_units",
            "observed_value": {"value": None},
        }],
    })
    assert project_snapshot_documents([document], snapshot) == [document]


def test_source_identity_cannot_be_overwritten_by_original_filename():
    document_id = str(uuid4())
    result = _source_ref(
        Document({}, "same.docx", document_id),
        {"source_ref": {"file": "same.docx", "document_id": "wrong-id", "row": 3}},
    )
    assert result["document_id"] == document_id
    assert result["file"] == "same.docx"


def test_recheck_projects_arbitrary_attribute_changes():
    document_id, entity_id, ref_id = [uuid4() for _ in range(3)]
    parsed = payload("RPD")
    parsed["competencies"] = [{
        "code": "УК-1", "formulation": "Старая формулировка",
        "source_ref": {"file": "source.docx", "paragraph_index": 1},
    }]
    document = {"document_id": str(document_id), "parsed": parsed}
    snapshot = SnapshotV2.model_validate({
        "document_ids": [document_id],
        "entities": [{"id": entity_id, "kind": "competency",
                      "attributes": {"formulation": "Новая формулировка"},
                      "source_ref_ids": [ref_id]}],
        "source_refs": [{
            "id": ref_id, "document_id": document_id,
            "locator": '{"file":"source.docx","paragraph_index":1}',
        }],
        "source_links": [{"id": uuid4(), "target_kind": "entity", "target_id": entity_id,
                          "field_path": "/attributes/formulation", "source_ref_id": ref_id,
                          "observed_value": {"value": "Старая формулировка"}}],
        "review": {
            "review_id": uuid4(), "import_job_id": uuid4(), "analysis_version_id": uuid4(),
            "graph_ready": True, "decisions": [{
                "conflict_id": uuid4(), "variant_id": uuid4(), "check_job_id": uuid4(),
                "changes": [{"op": "set_attribute", "target_kind": "entity",
                             "target_id": str(entity_id),
                             "field_path": "/attributes/formulation",
                             "value": "Новая формулировка"}],
                "decided_at": "2026-11-17T00:00:00Z",
            }],
        },
    })
    projected = project_snapshot_documents([document], snapshot)
    assert projected[0]["parsed"]["competencies"][0]["formulation"] == "Новая формулировка"


def test_recheck_rejects_attribute_value_that_breaks_document_contract():
    document_id, entity_id, ref_id = [uuid4() for _ in range(3)]
    parsed = {
        "meta": {
            "schema_version": 2, "doc_type": "CKMV", "file_name": "source.docx",
            "file_hash": "a" * 64, "parser_config": "test",
        },
        "competencies": [{
            "code": "УК-1", "formulation": "Исходная формулировка",
            "source_ref": {"file": "source.docx", "row": 1},
        }],
    }
    snapshot = SnapshotV2.model_validate({
        "document_ids": [document_id],
        "entities": [{
            "id": entity_id, "kind": "competency",
            "attributes": {"formulation": 123}, "source_ref_ids": [ref_id],
        }],
        "source_refs": [{
            "id": ref_id, "document_id": document_id,
            "locator": '{"file":"source.docx","row":1}',
        }],
        "source_links": [{
            "id": uuid4(), "target_kind": "entity", "target_id": entity_id,
            "field_path": "/attributes/formulation", "source_ref_id": ref_id,
            "observed_value": {"value": "Исходная формулировка"},
        }],
        "review": {
            "review_id": uuid4(), "import_job_id": uuid4(),
            "analysis_version_id": uuid4(), "graph_ready": True,
            "decisions": [{
                "conflict_id": uuid4(), "variant_id": uuid4(), "check_job_id": uuid4(),
                "changes": [{
                    "op": "set_attribute", "target_kind": "entity",
                    "target_id": str(entity_id), "field_path": "/attributes/formulation",
                    "value": 123,
                }],
                "decided_at": "2026-11-17T00:00:00Z",
            }],
        },
    })

    with pytest.raises(ValueError, match="контракт исходного документа"):
        project_snapshot_documents(
            [{"document_id": str(document_id), "parsed": parsed}], snapshot
        )


@pytest.mark.parametrize("new_value", ["144", True])
def test_recheck_rejects_non_numeric_supported_values(new_value):
    document_id, entity_id, ref_id = [uuid4() for _ in range(3)]
    document = {"document_id": str(document_id), "parsed": {
        "meta": {"doc_type": "RPD"},
        "disciplines": [{"name": "Алгебра", "hours_total": 108, "source_ref": {"row": 2}}],
    }}
    snapshot = SnapshotV2.model_validate({
        "document_ids": [document_id],
        "entities": [{
            "id": entity_id,
            "kind": "discipline",
            "attributes": {"hours_total": new_value},
            "source_ref_ids": [ref_id],
        }],
        "source_refs": [{"id": ref_id, "document_id": document_id, "locator": '{"row":2}'}],
        "source_links": [{
            "id": uuid4(), "target_kind": "entity", "target_id": entity_id,
            "field_path": "/attributes/hours_total", "source_ref_id": ref_id,
            "observed_value": {"value": 108},
        }],
        "review": {
            "review_id": uuid4(), "import_job_id": uuid4(), "analysis_version_id": uuid4(),
            "graph_ready": True, "decisions": [{
                "conflict_id": uuid4(), "variant_id": uuid4(), "check_job_id": uuid4(),
                "changes": [{
                    "op": "set_attribute", "target_kind": "entity",
                    "target_id": str(entity_id), "field_path": "/attributes/hours_total",
                    "value": new_value,
                }], "decided_at": "2026-11-17T00:00:00Z",
            }],
        },
    })
    with pytest.raises(ValueError, match="конечное числовое значение"):
        project_snapshot_documents([document], snapshot)


def test_old_local_ruleset_is_rejected_after_implementation_changes(monkeypatch):
    document_id = str(uuid4())
    parsed = {"meta": {"doc_type": "RPD", "program_id": "P"}}
    inputs = [Document(parsed, document_id, document_id)]
    ruleset = validation_ruleset(load_rules(), inputs)
    monkeypatch.setattr(validation_bridge, "ADAPTER_REVISION", "incompatible-v2")
    with pytest.raises(ValueError, match="несовместима"):
        run_local_check(
            [{"document_id": document_id, "parsed": parsed}],
            SnapshotV2(document_ids=[document_id]), uuid4(),
            config=load_rules(), ruleset=ruleset,
        )


def test_confirmation_policy_is_included_in_ruleset_revision(monkeypatch):
    document_id = str(uuid4())
    inputs = [Document({"meta": {"doc_type": "RPD"}}, document_id, document_id)]
    initial = validation_ruleset(load_rules(), inputs)
    monkeypatch.setattr(
        validation_bridge,
        "CONFIRMATION_POLICY",
        {**validation_bridge.CONFIRMATION_POLICY, "revision": "test-policy-v2"},
    )
    updated = validation_ruleset(load_rules(), inputs)
    assert initial["revision"] != updated["revision"]


def test_llm_budget_stops_calls_at_the_global_limit(monkeypatch):
    monkeypatch.setattr(validation_bridge, "VALIDATION_MAX_LLM_CALLS", 1)
    budget = _ValidationBudget()
    seen_timeouts = []

    def evaluate(_payload, config):
        seen_timeouts.append(config["timeout_seconds"])
        return {"status": "ok"}

    assert budget.evaluate(evaluate, {}, {"timeout_seconds": 90}) == {"status": "ok"}
    with pytest.raises(RuntimeError, match="лимит запросов"):
        budget.evaluate(evaluate, {}, {})
    assert budget.calls == 1
    assert validation_bridge.VALIDATION_LLM_CALL_TIMEOUT_SECONDS == 180
    assert seen_timeouts[0] == 90


def test_llm_budget_stops_at_the_global_deadline():
    budget = _ValidationBudget()
    budget.deadline = 0
    invoked = False

    def evaluate(_payload, _config):
        nonlocal invoked
        invoked = True
        return {}

    with pytest.raises(RuntimeError, match="бюджет времени"):
        budget.evaluate(evaluate, {}, {})
    assert not invoked


def test_llm_budget_stops_repeating_calls_after_rate_limit_exhaustion(monkeypatch):
    from worker.validation.semantic_llm import SemanticEvaluationUnavailable

    monkeypatch.setattr(validation_bridge, "VALIDATION_MAX_LLM_CALLS", 10)
    monkeypatch.setattr(validation_bridge, "_rate_limit_retry_delay", lambda *_args: 0)
    monkeypatch.setattr(validation_bridge, "_defer_llm_requests", lambda *_args: None)
    monkeypatch.setattr(validation_bridge.time, "sleep", lambda _seconds: None)
    budget = _ValidationBudget()
    calls = 0

    class TooManyRequests(Exception):
        status_code = 429

    def rate_limited(_payload, _config):
        nonlocal calls
        calls += 1
        raise TooManyRequests()

    with pytest.raises(TooManyRequests):
        budget.evaluate(rate_limited, {}, {"rate_limit_retries": 2})
    assert calls == 3

    with pytest.raises(SemanticEvaluationUnavailable, match="HTTP 429"):
        budget.evaluate(rate_limited, {}, {"rate_limit_retries": 2})
    assert calls == 3


def test_llm_budget_preserves_semantic_prompt_and_caps_call_timeout(monkeypatch):
    from worker.validation import semantic_llm

    seen = {}

    def evaluate(payload, config, *, system_prompt, evaluator):
        seen.update(system_prompt=system_prompt, timeout=config["timeout_seconds"])
        return {"ok": True}

    monkeypatch.setattr(semantic_llm, "evaluate_semantic_payload", evaluate)
    budget = _ValidationBudget()
    assert budget.semantic(
        {}, {"timeout_seconds": 100, "_system_prompt": "Исходная инструкция"}
    ) == {"ok": True}
    assert seen["system_prompt"] == "Исходная инструкция"
    assert seen["timeout"] <= validation_bridge.VALIDATION_LLM_CALL_TIMEOUT_SECONDS


def test_r07_keeps_manual_choice_without_using_llm_budget(monkeypatch):
    monkeypatch.setattr(validation_bridge, "VALIDATION_MAX_LLM_CALLS", 0)

    def unexpected_llm_call(*args, **kwargs):
        raise AssertionError("R-07 must not call an LLM")

    monkeypatch.setattr(
        validation_bridge._ValidationBudget, "semantic", unexpected_llm_call
    )
    document_ids, source_refs = [uuid4(), uuid4()], [uuid4(), uuid4()]
    documents = [
        {
            "document_id": str(document_id),
            "parsed": {
                "meta": {
                    "doc_type": doc_type,
                    "program_id": "P-1",
                    "file_name": f"{doc_type}.docx",
                },
                "competencies": [{
                    "code": "УК-1",
                    "formulation": formulation,
                    "source_ref": {"row": 1},
                }],
            },
        }
        for document_id, doc_type, formulation in zip(
            document_ids,
            ("CHARACTERISTIC", "RPD"),
            ("Использовать методы анализа", "Применять методы анализа"),
            strict=True,
        )
    ]
    snapshot = SnapshotV2.model_validate({
        "document_ids": document_ids,
        "source_refs": [
            {"id": ref_id, "document_id": document_id, "locator": '{"row":1}'}
            for ref_id, document_id in zip(source_refs, document_ids, strict=True)
        ],
    })
    config = {
        "schema_version": 2,
        "phases": {
            "document_reconciliation": {"input": "parsed_documents"},
            "graph_validation": {"input": "graph_projection"},
        },
        "rules": [{
        "id": "R-07",
        "name": "Сравнение формулировок",
        "type": "competency_formulations",
        "status": "ready",
        "phases": ["document_reconciliation", "graph_validation"],
    }]}

    result = run_local_check(documents, snapshot, uuid4(), config=config)

    r07_coverage = next(item for item in result["coverage"] if item["rule_code"] == "R-07")
    r07_issue = next(item for item in result["issues"] if item["rule_code"] == "R-07")
    assert r07_coverage["status"] == "needs_review"
    assert r07_issue["status"] == "needs_review"
    assert r07_issue["evidence"]["requires_user_confirmation"] is True
    assert len(r07_issue["evidence"]["variants"]) == 2
    assert "comparisons" not in r07_issue["evidence"]
    assert len(r07_issue["source_ref_ids"]) == 2


def test_recheck_projects_supported_field_edits_without_mutating_parser_input():
    from app.validation_bridge import project_snapshot_documents

    document_id, entity_id, ref_id, link_id = [uuid4() for _ in range(4)]
    parsed = payload("RPD")
    original = {"document_id": str(document_id), "parsed": parsed}
    locator = json.dumps(parsed["disciplines"][0]["source_ref"])
    snapshot = SnapshotV2.model_validate(
        {
            "document_ids": [document_id],
            "entities": [
                {
                    "id": entity_id,
                    "kind": "discipline",
                    "attributes": {"hours_total": 144},
                    "source_ref_ids": [ref_id],
                }
            ],
            "relations": [],
            "source_refs": [{"id": ref_id, "document_id": document_id, "locator": locator}],
            "source_links": [
                {
                    "id": link_id,
                    "target_kind": "entity",
                    "target_id": entity_id,
                    "field_path": "/attributes/hours_total",
                    "source_ref_id": ref_id,
                    "observed_value": {"value": 108},
                }
            ],
            "review": {
                "review_id": uuid4(),
                "import_job_id": uuid4(),
                "analysis_version_id": uuid4(),
                "graph_ready": True,
                "decisions": [{
                    "conflict_id": uuid4(),
                    "variant_id": uuid4(),
                    "check_job_id": uuid4(),
                    "changes": [{
                        "op": "set_attribute",
                        "target_kind": "entity",
                        "target_id": str(entity_id),
                        "field_path": "/attributes/hours_total",
                        "value": 144,
                    }],
                    "decided_at": "2026-11-17T00:00:00Z",
                }],
            },
        }
    )
    projected = project_snapshot_documents([original], snapshot)
    assert projected[0]["parsed"]["disciplines"][0]["hours_total"] == 144
    assert original["parsed"]["disciplines"][0]["hours_total"] == 108

    snapshot.entities[0].attributes["hours_total"] = True
    snapshot.source_links[0].observed_value["value"] = 1
    with pytest.raises(ValueError, match="числовое значение"):
        project_snapshot_documents([original], snapshot)


@pytest.mark.parametrize("chat_value", [180, 108])
def test_recheck_keeps_document_choice_and_projects_final_chat_value(chat_value):
    from app.validation_bridge import project_snapshot_documents

    document_id, entity_id, ref_id, link_id = [uuid4() for _ in range(4)]
    review_id, import_id, analysis_id = [uuid4() for _ in range(3)]
    parsed = payload("RPD")
    original = {"document_id": str(document_id), "parsed": parsed}
    locator = json.dumps(parsed["disciplines"][0]["source_ref"])
    snapshot = SnapshotV2.model_validate(
        {
            "document_ids": [document_id],
            "entities": [
                {
                    "id": entity_id,
                    "kind": "discipline",
                    "attributes": {"hours_total": chat_value},
                    "source_ref_ids": [ref_id],
                }
            ],
            "relations": [],
            "source_refs": [{"id": ref_id, "document_id": document_id, "locator": locator}],
            "source_links": [
                {
                    "id": link_id,
                    "target_kind": "entity",
                    "target_id": entity_id,
                    "field_path": "/attributes/hours_total",
                    "source_ref_id": ref_id,
                    "observed_value": {"value": 108},
                }
            ],
            "review": {
                "review_id": review_id,
                "import_job_id": import_id,
                "analysis_version_id": analysis_id,
                "graph_ready": True,
                "decisions": [
                    {
                        "conflict_id": uuid4(),
                        "variant_id": uuid4(),
                        "check_job_id": review_id,
                        "changes": [
                            {
                                "op": "set_document_value",
                                "document_id": str(document_id),
                                "field_path": "/disciplines/0/hours_total",
                                "expected_old_value": 108,
                                "value": 144,
                            }
                        ],
                        "decided_at": "2026-11-16T00:00:00Z",
                    },
                    {
                        "conflict_id": uuid4(),
                        "variant_id": uuid4(),
                        "check_job_id": review_id,
                        "changes": [
                            {
                                "op": "set_attribute",
                                "target_kind": "entity",
                                "target_id": str(entity_id),
                                "field_path": "/attributes/hours_total",
                                "expected_old_value": 144,
                                "value": chat_value,
                            }
                        ],
                        "decided_at": "2026-11-16T00:01:00Z",
                    },
                ],
            },
        }
    )

    projected = project_snapshot_documents([original], snapshot)

    assert projected[0]["parsed"]["disciplines"][0]["hours_total"] == chat_value
    assert original["parsed"]["disciplines"][0]["hours_total"] == 108


def test_recheck_keeps_supported_document_choice_for_other_source_fields():
    from app.validation_bridge import project_snapshot_documents

    document_id, entity_id, ref_id, link_id = [uuid4() for _ in range(4)]
    review_id, import_id, analysis_id = [uuid4() for _ in range(3)]
    parsed = payload("RPD")
    parsed["competencies"] = [{
        "code": "УК-1", "formulation": "Исходная",
        "source_ref": {"file": "source.docx", "paragraph_index": 1},
    }]
    original = {"document_id": str(document_id), "parsed": parsed}
    snapshot = SnapshotV2.model_validate(
        {
            "document_ids": [document_id],
            "entities": [
                {
                    "id": entity_id,
                    "kind": "competency",
                    "attributes": {"formulation": "Выбранная"},
                    "source_ref_ids": [ref_id],
                }
            ],
            "relations": [],
            "source_refs": [{
                "id": ref_id, "document_id": document_id,
                "locator": '{"file":"source.docx","paragraph_index":1}',
            }],
            "source_links": [
                {
                    "id": link_id,
                    "target_kind": "entity",
                    "target_id": entity_id,
                    "field_path": "/attributes/formulation",
                    "source_ref_id": ref_id,
                    "observed_value": {"value": "Исходная"},
                }
            ],
            "review": {
                "review_id": review_id,
                "import_job_id": import_id,
                "analysis_version_id": analysis_id,
                "graph_ready": True,
                "decisions": [
                    {
                        "conflict_id": uuid4(),
                        "variant_id": uuid4(),
                        "check_job_id": review_id,
                        "changes": [
                            {
                                "op": "set_document_value",
                                "document_id": str(document_id),
                                "field_path": "/competencies/0/formulation",
                                "expected_old_value": "Исходная",
                                "value": "Выбранная",
                            }
                        ],
                        "decided_at": "2026-11-16T00:00:00Z",
                    }
                ],
            },
        }
    )

    projected = project_snapshot_documents([original], snapshot)

    assert projected[0]["parsed"]["competencies"][0]["formulation"] == "Выбранная"


def test_semantic_model_requests_disable_hidden_retries(monkeypatch):
    import importlib
    import sys
    from types import ModuleType, SimpleNamespace

    seen = {}

    def completion(**kwargs):
        seen.update(kwargs)
        return SimpleNamespace(choices=[SimpleNamespace(
            message=SimpleNamespace(content='{"status":"equivalent","reason":"совпадает"}')
        )])

    fake_litellm = ModuleType("litellm")
    fake_litellm.completion = completion
    fake_litellm.__path__ = []
    fake_exceptions = ModuleType("litellm.exceptions")
    fake_exceptions.RateLimitError = type("RateLimitError", (Exception,), {})
    fake_litellm.exceptions = fake_exceptions
    monkeypatch.setitem(sys.modules, "litellm", fake_litellm)
    monkeypatch.setitem(sys.modules, "litellm.exceptions", fake_exceptions)
    monkeypatch.setitem(sys.modules, "dotenv", SimpleNamespace(load_dotenv=lambda *a, **kw: None))
    monkeypatch.setenv("VALIDATION_LLM_MODEL", "test-model")
    monkeypatch.setenv("VALIDATION_LLM_API_KEY", "test-key")
    module = importlib.import_module("worker.validation.semantic_llm")
    module.evaluate_semantic_payload(
        {},
        {
            "timeout_seconds": 2,
            "max_completion_tokens": 8000,
            "response_format": {"type": "json_object"},
            "reasoning_effort": "low",
        },
        system_prompt="Проверка",
    )
    assert seen["timeout"] == 2
    assert seen.get("num_retries") == 0
    assert seen["max_completion_tokens"] == 8000
    assert seen["response_format"] == {"type": "json_object"}
    assert seen["reasoning_effort"] == "low"
