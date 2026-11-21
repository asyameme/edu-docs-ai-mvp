"""Только тестовый HTTP-сервер контракта. Это не реализация ML-парсера."""

import asyncio
import hashlib
import json

from fastapi import FastAPI, Request, UploadFile
from fastapi.responses import JSONResponse

app = FastAPI()
attempts = {}
keys = {}
active_parallel_parses = 0
max_parallel_parses = 0


@app.post("/parse")
async def parse(file: UploadFile, request: Request):
    global active_parallel_parses, max_parallel_parses
    content = await file.read()
    if content.startswith(b"parallel-"):
        active_parallel_parses += 1
        max_parallel_parses = max(max_parallel_parses, active_parallel_parses)
        await asyncio.sleep(1.5)
        active_parallel_parses -= 1
    else:
        await asyncio.sleep(0.2)
    digest = hashlib.sha256(content).hexdigest()
    attempts[digest] = attempts.get(digest, 0) + 1
    keys.setdefault(digest, []).append(request.headers.get("Idempotency-Key"))
    if content.startswith(b"broken"):
        return JSONResponse(status_code=422, content={"detail": "Повреждённый тестовый документ"})
    if content.startswith((b"transient", b"unavailable")):
        if attempts[digest] < 2 or content.startswith(b"unavailable"):
            return JSONResponse(status_code=503, content={"detail": "Повторите запрос"})
    if content.startswith(b"wrong-hash"):
        digest = "0" * 64
    if content.startswith(b"contract-v2:"):
        output = json.loads(content[len(b"contract-v2:") :])
        output["meta"]["file_hash"] = digest
        output["meta"]["file_name"] = file.filename
        return output
    issues = []
    if content.startswith(b"warning"):
        issues = [{"category": "empty_cell", "message": "Пустая ячейка", "severity": "warning"}]
    disciplines = (
        []
        if content.startswith(b"empty")
        else [
            {
                "name": "Тестовая дисциплина",
                "semesters": [1],
                "credit_units": 3,
                "hours_total": 108,
                "hours_by_kind": {"лекции": 108},
                "source_ref": {"file": file.filename, "section": "2", "paragraph_index": 1},
            }
        ]
    )
    return {
        "meta": {
            "schema_version": 2,
            "doc_type": "CURRICULUM" if content.startswith(b"empty") else "RPD",
            "file_name": file.filename,
            "file_hash": digest,
            "parser_config": "artificial-http-contract-fixture",
            "program_id": "26/5003/1",
        },
        **({"disciplines": disciplines} if disciplines else {}),
        **(
            {
                "competencies": [
                    {
                        "code": "УК-1",
                        "formulation": "Общепрофессиональная компетенция",
                        "source_ref": {
                            "file": file.filename,
                            "section": "3",
                            "paragraph_index": 1,
                        },
                    }
                ],
                "relations": [
                    {
                        "src_entity": "discipline",
                        "src_key": "Тестовая дисциплина",
                        "dst_entity": "competency",
                        "dst_key": "УК-404" if "unresolved" in file.filename else "УК-1",
                        "kind": "формирует",
                        "source_ref": {
                            "file": file.filename,
                            "section": "4",
                            "paragraph_index": 1,
                        },
                    }
                ],
            }
            if disciplines
            else {}
        ),
        "parse_issues": issues,
    }


@app.get("/calls/{digest}")
def calls(digest: str):
    return {"count": attempts.get(digest, 0), "keys": keys.get(digest, [])}


@app.post("/parallel-stats/reset")
async def reset_parallel_stats():
    global active_parallel_parses, max_parallel_parses
    active_parallel_parses = 0
    max_parallel_parses = 0
    return {"active": active_parallel_parses, "max_active": max_parallel_parses}


@app.get("/parallel-stats")
async def parallel_stats():
    return {"active": active_parallel_parses, "max_active": max_parallel_parses}
