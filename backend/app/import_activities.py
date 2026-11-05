"""HTTP-адаптер ML: Worker получает неизменяемые оригиналы из PostgreSQL."""

import io
import json
from uuid import UUID

import httpx
from pydantic import ValidationError
from temporalio import activity
from temporalio.exceptions import ApplicationError

from app.domain import Contract
from app.import_repository import ImportRepository
from app.parser_adapter import to_snapshot
from app.repository import Repository
from contracts.parser_document import has_document_data, parse_output


class ImportActivities:
    def __init__(self, pool, settings):
        self.repo = ImportRepository(pool)
        self.settings = settings

    def job(self, job_id):
        return self.repo.job(UUID(job_id), include_output=True)

    @activity.defn(name="import_begin")
    def begin(self, request: dict) -> list[str]:
        job_id = request["job_id"]
        job = self.job(job_id)
        if job["attempt"] != request["attempt"] or job["_terminal"]:
            return []
        return [str(item["document_id"]) for item in job["files"]]

    @activity.defn(name="import_parse_file")
    def parse(self, request: dict) -> None:
        job_id, document_id = request["job_id"], request["document_id"]
        attempt = request["attempt"]
        job = self.job(job_id)
        if job["attempt"] != attempt:
            return
        item = next(f for f in job["files"] if str(f["document_id"]) == document_id)
        if item["status"] in {"completed", "failed"}:
            return
        if not self.settings.parser_url:
            self.repo.file_finished(
                UUID(job_id),
                UUID(document_id),
                attempt,
                code="parser_not_configured",
                message="Сервис разбора документов ещё не подключён; оригинал сохранён",
            )
            return
        try:
            document = Repository(self.repo.pool).document_content(job["program_id"], document_id)
            with (
                io.BytesIO(document["content"]) as source,
                httpx.Client(timeout=90, trust_env=False) as client,
            ):
                with client.stream(
                    "POST",
                    self.settings.parser_url,
                    files={"file": (item["member_name"], source)},
                    data={"document_id": document_id, "program_id": str(job["program_id"])},
                    headers={"Idempotency-Key": f"{job_id}:{attempt}:{document_id}"},
                ) as response:
                    if response.status_code >= 500 or response.status_code == 429:
                        raise ApplicationError("Парсер временно недоступен", type="ParserTemporary")
                    response.raise_for_status()
                    chunks, size = [], 0
                    for chunk in response.iter_bytes():
                        size += len(chunk)
                        if size > 10 * 1024 * 1024:
                            raise ValueError("Ответ парсера превышает 10 МиБ")
                        chunks.append(chunk)
            raw = json.loads(b"".join(chunks))
            Contract.strings_are_storable(raw)
            parsed = parse_output(raw)
            if parsed.meta.file_hash != item["sha256"]:
                raise ValueError("Хэш результата не совпал с оригиналом")
            try:
                claimed_program_id = UUID(parsed.meta.program_id or "")
            except ValueError:
                claimed_program_id = None
            if claimed_program_id is not None and claimed_program_id != UUID(
                str(job["program_id"])
            ):
                raise ValueError("Результат документа принадлежит другой программе")
            if not has_document_data(parsed):
                self.repo.file_finished(
                    UUID(job_id),
                    UUID(document_id),
                    attempt,
                    code="parser_no_data",
                    message="В документе не найдено данных; оригинал сохранён",
                )
                return
            self.repo.file_finished(
                UUID(job_id),
                UUID(document_id),
                attempt,
                output=raw,
            )
        except httpx.TransportError as error:
            raise ApplicationError(
                "Не удалось связаться с ML-парсером",
                type="ParserTemporary",
            ) from error
        except (httpx.HTTPStatusError, ValidationError, ValueError, OSError):
            self.repo.file_finished(
                UUID(job_id),
                UUID(document_id),
                attempt,
                code="parser_invalid_result",
                message="Документ не прочитан или ответ парсера не соответствует контракту",
            )

    @activity.defn(name="import_file_failed")
    def failed(self, request: dict) -> None:
        self.repo.file_finished(
            UUID(request["job_id"]),
            UUID(request["document_id"]),
            request["attempt"],
            code="processing_failed",
            message="Обработка не завершилась после повторных попыток; оригинал сохранён",
        )

    @activity.defn(name="import_finish")
    def finish(self, request: dict) -> None:
        job_id = request["job_id"]
        job = self.job(job_id)
        if job["attempt"] != request["attempt"] or job["status"] in {
            "completed",
            "partial",
            "failed",
        }:
            return
        successful = [f for f in job["files"] if f["status"] == "completed"]
        snapshot, issues = to_snapshot(job["program_id"], job["files"])
        for item in job["files"]:
            if item["error_code"]:
                issues.append(
                    {
                        "category": item["error_code"],
                        "severity": "error",
                        "document_id": str(item["document_id"]),
                        "message": item["error_message"],
                    }
                )
        status = "failed" if not successful else ("partial" if issues else "completed")
        self.repo.finish(
            UUID(job_id),
            request["attempt"],
            status,
            snapshot if successful else None,
            issues,
        )

    @activity.defn(name="import_prepare_v2")
    def prepare_v2(self, request: dict) -> list[dict]:
        job = self.job(request["job_id"])
        if job["attempt"] != request["attempt"]:
            return []
        return [
            {"document_id": str(item["document_id"]), "status": item["status"]}
            for item in job["files"]
        ]

    @activity.defn(name="import_parse_file_v2")
    def parse_v2(self, request: dict) -> dict:
        self.parse(request)
        job = self.job(request["job_id"])
        item = next(f for f in job["files"] if str(f["document_id"]) == request["document_id"])
        return {"document_id": str(item["document_id"]), "status": item["status"]}

    @activity.defn(name="import_finish_v2")
    def finish_v2(self, request: dict) -> str:
        self.finish(request)
        return self.job(request["job_id"])["status"]
