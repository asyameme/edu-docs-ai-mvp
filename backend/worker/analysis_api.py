"""HTTP adapter for document validation and the Review API contracts."""
from __future__ import annotations

import json
from uuid import UUID, uuid5

from fastapi import FastAPI

from app.document_adapter import to_snapshot_v2
from app.review_contract import AnalysisV1, CheckRequest, CheckResult, PrepareRequest, Ruleset
from worker.validation.validator import run_validation

app = FastAPI(title="Education Document Analysis", version="0.2.0")


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


def _documents(candidate: dict, supplied: list | None = None) -> list[dict]:
    if supplied:
        return [document.parsed.model_dump(mode="json") for document in supplied]
    return list(candidate.get("metadata", {}).get("parsed_documents", {}).values())


def _source_ids(finding: dict, candidate: dict) -> list[str]:
    """Match the validator's {file, ...source_ref} to graph evidence.

    A filename must identify exactly one document; otherwise we refuse to
    attach an ambiguous source. The validator uses document-N as a fallback.
    """
    evidence = finding.get("evidence") or {}
    refs = candidate.get("source_refs", [])
    parsed = candidate.get("metadata", {}).get("parsed_documents", {})
    available = {str(ref["id"]) for ref in refs}
    result = {str(value) for value in evidence.get("source_ref_ids", []) if str(value) in available}
    labels = {}
    for index, (document_id, document) in enumerate(parsed.items(), 1):
        meta = document.get("meta") or {}
        label = meta.get("file_name") or f"document-{index}"
        labels.setdefault(label, []).append(str(document_id))
    for raw in evidence.get("source_refs", []):
        if not isinstance(raw, dict):
            continue
        document_ids = labels.get(raw.get("file"), [])
        if len(document_ids) != 1:
            continue
        locator = {key: value for key, value in raw.items() if key != "file"}
        for ref in refs:
            if str(ref["document_id"]) != document_ids[0]:
                continue
            try:
                value = json.loads(ref["locator"])
            except (ValueError, TypeError):
                continue
            if value == locator:
                result.add(str(ref["id"]))
    return sorted(result)


def _alias_pair(finding: dict) -> tuple[str, str] | None:
    if finding.get("code") != "competency_code_alias_review":
        return None
    evidence = finding.get("evidence") or {}
    a, b = evidence.get("code"), evidence.get("suggested_code")
    if not (a and b):
        pairs = evidence.get("suggested_pairs") or []
        if len(pairs) != 1:
            return None
        a, b = pairs[0].get("source_code"), pairs[0].get("suggested_code")
    return (str(a), str(b)) if a and b and a != b else None


def _issue_id(job_id: str, rule: str, index: int, finding: dict, candidate: dict,
              source_ids: list[str]) -> str:
    program_id = candidate.get("metadata", {}).get("program_id")
    if not program_id:
        return str(uuid5(UUID(job_id), f"{rule}:{index}"))
    ref_set = set(source_ids)
    sources = sorted(
        ({"document_id": str(ref["document_id"]), "locator": ref["locator"]}
         for ref in candidate.get("source_refs", []) if str(ref["id"]) in ref_set),
        key=lambda ref: (ref["document_id"], ref["locator"]),
    )
    if not sources:
        # Retain a stable identifier for evidence that cannot be represented
        # by a graph source (e.g. a dangling relation).
        sources = sorted((finding.get("evidence") or {}).get("source_refs", []),
                         key=lambda ref: json.dumps(ref, sort_keys=True, ensure_ascii=False))
    if not sources:
        evidence = finding.get("evidence") or {}
        identity = {
            "rule": rule,
            "type": finding.get("code"),
            "evidence": evidence,
        }
        return str(uuid5(
            UUID(str(program_id)),
            json.dumps(identity, sort_keys=True, ensure_ascii=False, default=str),
        ))
    pair = _alias_pair(finding)
    identity = {"rule": rule, "type": finding.get("code"), "sources": sources,
                "code": (finding.get("evidence") or {}).get("code"),
                "pair": sorted(pair) if pair else None}
    return str(uuid5(UUID(str(program_id)), json.dumps(identity, sort_keys=True, ensure_ascii=False)))


def _related_objects(candidate: dict, source_ids: list[str], kind: str) -> list[dict]:
    wanted = set(source_ids)
    return [item for item in candidate.get("entities" if kind == "entity" else "relations", [])
            if wanted.intersection(map(str, item.get("source_ref_ids", [])))]


def _alias_variants(issue_id: str, finding: dict, candidate: dict,
                    source_ids: list[str]) -> list[dict]:
    pair = _alias_pair(finding)
    if not pair or finding.get("status") != "needs_review":
        return []
    source_code, target_code = pair
    # An alias is a reviewer assertion, NOT a safe instruction to merge graph nodes.
    # Record the decision on the specific source relations or entities, preserving
    # original codes, node IDs and source evidence.
    relations = [item for item in _related_objects(candidate, source_ids, "relation")
                 if item.get("attributes", {}).get("src_key") == source_code
                 or item.get("attributes", {}).get("dst_key") == source_code]
    entities = [item for item in _related_objects(candidate, source_ids, "entity")
                if item.get("kind") == "competency"
                and item.get("attributes", {}).get("code") == source_code]
    targets = [("relation", item) for item in relations] or [("entity", item) for item in entities]
    namespace = UUID(issue_id)
    variants = []
    if targets:
        changes = [{"op": "set_attribute", "target_kind": kind, "target_id": item["id"],
                    "field_path": "/attributes/competency_alias_review",
                    "value": {"source_code": source_code, "suggested_code": target_code}}
                   for kind, item in targets]
        variants.append({"id": str(uuid5(namespace, "accept-alias")),
                         "label": f"Подтвердить соответствие {source_code} → {target_code} (без объединения узлов)",
                         "changes": changes})
    variants.append({"id": str(uuid5(namespace, "reject-alias")),
                     "label": f"Оставить {source_code} и {target_code} разными", "changes": []})
    return variants


def _formulation_variants(issue_id: str, finding: dict, candidate: dict) -> list[dict]:
    evidence = finding.get("evidence") or {}
    if finding.get("code") != "competency_formulation_review":
        return []
    code = evidence.get("code")
    candidates = sorted({v.get("formulation") for v in evidence.get("variants", [])
                         if isinstance(v.get("formulation"), str) and v["formulation"].strip()})
    if len(candidates) < 2:
        return []
    entities = [e for e in candidate.get("entities", []) if e.get("kind") == "competency"
                and e.get("attributes", {}).get("code") == code]
    if not entities:
        return []
    variants = []
    for text in candidates:
        changes = [{"op": "set_attribute", "target_kind": "entity", "target_id": e["id"],
                    "field_path": "/attributes/formulation", "value": text}
                   for e in entities]
        if changes:
            variants.append({"id": str(uuid5(UUID(issue_id), "formulation:" + text)),
                             "label": f"Выбрать формулировку: {text[:100]}", "changes": changes})
    return variants


def _issue(job_id: str, rule: str, index: int, finding: dict, candidate: dict) -> dict:
    source_ids = _source_ids(finding, candidate)
    issue_id = _issue_id(job_id, rule, index, finding, candidate, source_ids)
    variants = (_alias_variants(issue_id, finding, candidate, source_ids)
                if rule == "R-01" else
                _formulation_variants(issue_id, finding, candidate) if rule == "R-07" else [])
    return {"id": issue_id, "rule_code": rule, "rule_revision": "1",
            "type": finding.get("code", "validation_finding"),
            "severity": "blocking" if finding.get("status") == "failed" else "warning",
            "status": "needs_review" if finding.get("status") == "needs_review" else "open",
            "message": finding.get("message") or "Обнаружено несоответствие",
            "source_ref_ids": source_ids, "requires_decision": bool(variants),
            "variants": variants}


def _convert(job_id: str, report: dict, candidate: dict) -> tuple[list[dict], list[dict]]:
    coverage, issues = [], []
    for result in report.get("results", []):
        findings = result.get("findings", [])
        rule = result["rule_id"]
        issues.extend(_issue(job_id, rule, index, finding, candidate)
                      for index, finding in enumerate(findings))
        status = result.get("status")
        # The Review API has no needs_review coverage state. Any finding is
        # failed coverage; the issue severity determines whether it blocks.
        mapped = "failed" if findings else ("passed" if status == "passed" else "skipped")
        coverage.append({"rule_code": rule, "rule_revision": "1", "status": mapped,
                         "reason": f"Замечаний: {len(findings)}" if findings else
                         ("Нет данных для проверки" if mapped == "skipped" else None)})
    return coverage, issues


def _reconcile(coverage: list[dict], issues: list[dict], snapshot: dict) -> tuple[list[dict], list[dict]]:
    decisions = {str(d["conflict_id"]): d for d in (snapshot.get("review") or {}).get("decisions", [])}
    filtered = []
    for issue in issues:
        decision = decisions.get(issue["id"])
        if not decision:
            filtered.append(issue)
            continue
        selected = next((v for v in issue["variants"] if v["id"] == str(decision["variant_id"])), None)
        if selected is None or selected["changes"] != decision.get("changes", []):
            filtered.append(issue)
            continue
        if selected["changes"]:
            # Verify all intended attribute mutations are present in the working graph.
            by_id = {item["id"]: item for item in snapshot.get("entities", []) + snapshot.get("relations", [])}
            if not all(by_id.get(c["target_id"], {}).get("attributes", {}).get(
                c["field_path"].removeprefix("/attributes/")) == c["value"]
                for c in selected["changes"] if c["op"] == "set_attribute"):
                filtered.append(issue)
                continue
        # An explicit reject decision resolves the suggestion, not the underlying
        # factual mismatch. Other findings are not suppressed.
        if issue["type"] not in {"competency_code_alias_review", "competency_formulation_review"}:
            filtered.append(issue)
    for item in coverage:
        if item["status"] == "failed" and not any(i["rule_code"] == item["rule_code"] for i in filtered):
            item["status"] = "passed"
            item["reason"] = "Замечания рассмотрены; решения сохранены в рабочей версии"
    return coverage, filtered


@app.post("/analyze", response_model=AnalysisV1)
def analyze_documents(request: PrepareRequest) -> AnalysisV1:
    candidate, _ = to_snapshot_v2(str(request.program_id), [
        {"document_id": str(doc.document_id), "output": doc.parsed.model_dump(mode="json")}
        for doc in request.documents])
    report = run_validation(_documents(candidate, request.documents)).to_dict()
    coverage, issues = _convert(str(request.job_id), report, candidate)
    ruleset = Ruleset(id="competency-validation", revision="1", config={},
                      required_rules=[c["rule_code"] for c in coverage],
                      required_document_types=sorted({d.parsed.meta.doc_type for d in request.documents}))
    return AnalysisV1.model_validate({"schema_version": 1, "candidate": candidate,
        "ruleset": ruleset.model_dump(mode="json"), "coverage": coverage,
        "issues": issues, "artifacts": []})


@app.post("/check", response_model=CheckResult)
def check_documents(request: CheckRequest) -> CheckResult:
    snapshot = request.snapshot.model_dump(mode="json")
    report = run_validation(_documents(snapshot, request.documents)).to_dict()
    coverage, issues = _convert(str(request.job_id), report, snapshot)
    coverage, issues = _reconcile(coverage, issues, snapshot)
    return CheckResult.model_validate({"schema_version": 1,
        "ruleset": request.ruleset.model_dump(mode="json"),
        "coverage": coverage, "issues": issues})
