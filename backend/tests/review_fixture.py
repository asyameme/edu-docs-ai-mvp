"""Искусственный HTTP-анализатор для интеграционных тестов; документы не извлекает."""

import asyncio
import hashlib
from uuid import UUID, uuid5

import httpx
from fastapi import FastAPI
from fastapi.responses import JSONResponse

from app.parser_adapter import to_snapshot
from app.review_logic import digest

app = FastAPI()
calls = {}
RULESET = {
    "id": "artificial-backend-review",
    "revision": "1",
    "config": {"artificial": True},
    "required_rules": ["hours"],
    "required_document_types": ["RPD"],
}


@app.post("/analyze")
async def analyze(request: dict):
    namespace = UUID(request["job_id"])
    calls[request["job_id"]] = calls.get(request["job_id"], 0) + 1
    documents = request["documents"]
    filename = documents[0]["parsed"]["meta"]["file_name"]
    if filename.startswith("slow"):
        await asyncio.sleep(3)
    if filename.startswith("transient-analysis") and calls[request["job_id"]] == 1:
        return JSONResponse(status_code=503, content={"detail": "Artificial temporary failure"})
    async with httpx.AsyncClient(timeout=10, trust_env=False) as client:
        for document in documents:
            if document.get("original_url"):
                original = await client.get(document["original_url"])
                if (
                    original.status_code != 200
                    or hashlib.sha256(original.content).hexdigest() != document["sha256"]
                ):
                    return JSONResponse(
                        status_code=422, content={"detail": "Original is unavailable or changed"}
                    )
    snapshot, _ = to_snapshot(
        request["program_id"],
        [{"document_id": item["document_id"], "output": item["parsed"]} for item in documents],
    )
    snapshot.update(schema_version=2, source_links=[], review=None)
    snapshot["metadata"]["fixture_case"] = filename
    issues = []
    for index in range(19 if filename.startswith("many") else 1):
        if filename.startswith("noconflict"):
            break
        issues.append(
            {
                "id": str(uuid5(namespace, f"conflict-{index}")),
                "rule_code": "hours",
                "rule_revision": "1",
                "type": "hours_difference",
                "severity": "blocking",
                "requires_decision": True,
                "message": "Искусственный конфликт часов",
                "source_ref_ids": [snapshot["source_refs"][0]["id"]],
                "variants": [
                    {
                        "id": str(uuid5(namespace, f"variant-{index}-{hours}")),
                        "label": str(hours),
                        "changes": [
                            {
                                "op": "set_attribute",
                                "target_kind": "entity",
                                "target_id": snapshot["entities"][0]["id"],
                                "field_path": "/attributes/hours_total",
                                "value": hours,
                            }
                        ],
                    }
                    for hours in (108, 144)
                ],
            }
        )
    artifacts = []
    for doc in documents:
        identity = str(uuid5(namespace, "markdown:" + doc["document_id"]))
        artifacts.append(
            {
                "id": identity,
                "document_id": doc["document_id"],
                "parser_revision": "artificial-1",
                "parser_config": {"artificial": True, "threshold": 1.0},
                "parser_config_hash": digest({"artificial": True, "threshold": 1.0}),
                "markdown": "# Искусственный Markdown\n",
            }
        )
        for ref in snapshot["source_refs"]:
            if ref["document_id"] == doc["document_id"]:
                ref.update(artifact_id=identity, artifact_locator="line:1")
    for entity in snapshot["entities"]:
        snapshot["source_links"].append(
            {
                "id": str(uuid5(namespace, "link:" + entity["id"])),
                "target_kind": "entity",
                "target_id": entity["id"],
                "field_path": "/attributes/hours_total",
                "source_ref_id": entity["source_ref_ids"][0],
                "observed_value": {"value": 108, "unit": "hour"},
            }
        )
    if filename.startswith("malformed"):
        snapshot["source_links"][0]["target_id"] = str(uuid5(namespace, "absent"))
    return {
        "schema_version": 1,
        "candidate": snapshot,
        "issues": issues,
        "ruleset": RULESET,
        "coverage": [
            {"rule_code": "hours", "rule_revision": "1", "status": "failed" if issues else "passed"}
        ],
        "artifacts": artifacts,
    }


@app.post("/check")
def check(request: dict):
    namespace = UUID(request["job_id"])
    case = request["snapshot"]["metadata"].get("fixture_case", "")
    issues = []
    if case.startswith(("warning", "blocking")):
        issues.append(
            {
                "id": str(uuid5(namespace, "check-issue")),
                "rule_code": "hours",
                "rule_revision": "1",
                "type": "example_check",
                "severity": "blocking" if case.startswith("blocking") else "warning",
                "message": "Искусственное замечание",
                "source_ref_ids": [],
            }
        )
    status = "skipped" if case.startswith("skipped") else ("failed" if issues else "passed")
    return {
        "schema_version": 1,
        "ruleset": request["ruleset"],
        "issues": issues,
        "coverage": [
            {
                "rule_code": "hours",
                "rule_revision": "1",
                "status": status,
                "reason": "Искусственный пропуск" if status == "skipped" else None,
            }
        ],
    }
