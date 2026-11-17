"""Искусственный HTTP-анализатор для интеграционных тестов; документы не извлекает."""

import asyncio
import hashlib
from uuid import UUID, uuid5

import httpx
from fastapi import FastAPI
from fastapi.responses import JSONResponse

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
    issues = []
    hours = documents[0]["parsed"]["disciplines"][0]["hours_total"]
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
                "source_document_ids": [documents[0]["document_id"]],
                "variants": [
                    {
                        "id": str(uuid5(namespace, f"variant-{index}-{value}")),
                        "label": str(value),
                        "changes": [
                            {
                                "op": "set_document_value",
                                "document_id": documents[0]["document_id"],
                                "field_path": "/disciplines/0/hours_total",
                                "expected_old_value": hours,
                                "value": value,
                            }
                        ],
                    }
                    for value in (108, 144)
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
    if filename.startswith("malformed"):
        return {
            "schema_version": 2,
            "candidate": {},
            "issues": [],
            "ruleset": RULESET,
            "coverage": [],
        }
    return {
        "schema_version": 2,
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
    case = request["documents"][0]["parsed"]["meta"]["file_name"]
    hours = request["documents"][0]["parsed"]["disciplines"][0]["hours_total"]
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
                "reason": (
                    "Искусственный пропуск"
                    if status == "skipped"
                    else f"Получено часов: {hours}"
                ),
            }
        ],
    }
