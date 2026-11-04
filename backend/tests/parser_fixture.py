"""Только тестовый HTTP-сервер контракта. Это не реализация ML-парсера."""

import asyncio
import hashlib

from fastapi import FastAPI, Request, UploadFile
from fastapi.responses import JSONResponse

app = FastAPI()
attempts = {}
keys = {}


@app.post("/parse")
async def parse(file: UploadFile, request: Request):
    content = await file.read()
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
                "source_ref": {"file": file.filename, "section": "2", "row": 1},
            }
        ]
    )
    return {
        "meta": {
            "doc_type": "RPD",
            "file_name": file.filename,
            "file_hash": digest,
            "parser_config": "artificial-http-contract-fixture",
        },
        "disciplines": disciplines,
        "parse_issues": issues,
    }


@app.get("/calls/{digest}")
def calls(digest: str):
    return {"count": attempts.get(digest, 0), "keys": keys.get(digest, [])}
