"""Local review semantics tests; no HTTP, PostgreSQL, Temporal or Docker."""
from copy import deepcopy
from uuid import uuid4

from backend.worker.analysis_api import _issue, _reconcile


def _entity(code, formulation, source_id):
    return {
        "id": str(uuid4()),
        "kind": "competency",
        "attributes": {"code": code, "formulation": formulation},
        "source_ref_ids": [source_id],
    }


def _finding(code, evidence):
    return {"code": code, "status": "needs_review", "message": "Review required", "evidence": evidence}


def _apply(snapshot, variant):
    updated = deepcopy(snapshot)
    objects = {item["id"]: item for item in updated["entities"] + updated["relations"]}
    for change in variant["changes"]:
        assert change["op"] == "set_attribute"
        assert change["field_path"].startswith("/attributes/")
        objects[change["target_id"]]["attributes"][change["field_path"].split("/", 2)[2]] = change["value"]
    return updated


def test_r01_offers_accept_and_reject_without_merging_codes():
    source = str(uuid4())
    entity = _entity("УНК-03", "Competency", source)
    candidate = {"entities": [entity], "relations": [], "source_refs": []}
    finding = _finding("competency_code_alias_review", {
        "code": "УНК-03", "suggested_code": "ПКП-УНК-03",
    })
    # The source-ref mapper may not resolve the artificial source: call the
    # variant builder directly with a known source reference.
    from backend.worker.analysis_api import _alias_variants
    variants = _alias_variants(str(uuid4()), finding, candidate, [source])
    assert len(variants) == 2
    accept = next(v for v in variants if v["changes"])
    reject = next(v for v in variants if not v["changes"])
    assert reject["changes"] == []
    updated = _apply(candidate, accept)
    assert updated["entities"][0]["attributes"]["code"] == "УНК-03"
    assert updated["entities"][0]["attributes"]["competency_alias_review"] == {
        "source_code": "УНК-03", "suggested_code": "ПКП-УНК-03"
    }


def test_r07_selected_formulation_stays_resolved_after_recheck():
    source_a, source_b = str(uuid4()), str(uuid4())
    candidate = {
        "entities": [_entity("УК-5", "First formulation", source_a),
                     _entity("УК-5", "Second formulation", source_b)],
        "relations": [], "source_refs": [],
    }
    finding = _finding("competency_formulation_review", {
        "code": "УК-5",
        "variants": [{"formulation": "First formulation"}, {"formulation": "Second formulation"}],
    })
    job_id = str(uuid4())
    initial = _issue(job_id, "R-07", 0, finding, candidate)
    assert len(initial["variants"]) == 2
    selected = next(v for v in initial["variants"]
                    if any(c["value"] == "First formulation" for c in v["changes"]))
    changed = _apply(candidate, selected)
    changed["review"] = {"decisions": [{
        "conflict_id": initial["id"], "variant_id": selected["id"],
        "changes": selected["changes"],
    }]}
    again = _issue(job_id, "R-07", 0, finding, changed)
    print("\n=== R-07 DIAGNOSTICS ===")
    print("Issue ID matches:", initial["id"] == again["id"])

    matching = [
        v for v in again["variants"]
        if v["id"] == selected["id"]
    ]

    print("Variant ID matches:", bool(matching))
    print(
        "Changes match:",
        bool(matching) and matching[0]["changes"] == selected["changes"]
    )

    print("Saved decision:", changed["review"]["decisions"])
    print("Selected variant:", selected)
    print("Recreated variants:", again["variants"])
    coverage = [{"rule_code": "R-07", "status": "failed", "reason": "Review required"}]
    resolved_coverage, remaining = _reconcile(coverage, [again], changed)
    assert remaining == [], "R-07 decision was not recognized after applying the selected formulation"
    assert resolved_coverage[0]["status"] == "passed"


def test_r01_rejected_suggestion_is_recognized_on_recheck():
    source = str(uuid4())
    candidate = {"entities": [_entity("УНК-03", "Text", source)], "relations": [], "source_refs": []}
    finding = _finding("competency_code_alias_review", {
        "code": "УНК-03", "suggested_code": "ПКП-УНК-03"
    })
    from backend.worker.analysis_api import _alias_variants, _reconcile
    issue_id = str(uuid4())
    variants = _alias_variants(issue_id, finding, candidate, [source])
    reject = next(v for v in variants if not v["changes"])
    issue = {"id": issue_id, "rule_code": "R-01", "type": "competency_code_alias_review", "variants": variants}
    candidate["review"] = {"decisions": [{"conflict_id": issue_id, "variant_id": reject["id"], "changes": []}]}
    coverage, issues = _reconcile([{"rule_code": "R-01", "status": "failed"}], [issue], candidate)
    assert issues == []
    assert coverage[0]["status"] == "passed"
