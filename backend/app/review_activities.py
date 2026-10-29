"""Ограниченный HTTP-обмен с анализатором и запись долговечного результата."""

import hashlib
import json
import os
from uuid import UUID, uuid5

import httpx
from pydantic import ValidationError
from temporalio import activity
from temporalio.exceptions import ApplicationError

from app.domain import SnapshotV2
from app.errors import DomainError
from app.review_contract import AnalysisV1, CheckRequest, CheckResult, PrepareRequest
from app.review_logic import apply_changes, digest, map_analysis_ids
from app.review_repository import ReviewRepository


class ReviewActivities:
    def __init__(self, pool, settings):
        self.repo = ReviewRepository(pool)
        self.settings = settings

    def save_artifacts(self, result):
        artifacts = []
        root = self.settings.artifact_files_path
        for artifact in result["artifacts"]:
            if digest(artifact["parser_config"]) != artifact["parser_config_hash"]:
                raise ValueError("Parser configuration hash mismatch")
            raw = artifact["markdown"].encode("utf-8")
            if len(raw) > self.settings.analysis_max_response_bytes:
                raise ValueError("Markdown artifact exceeds configured limit")
            identity = str(UUID(artifact["id"]))
            root.mkdir(parents=True, exist_ok=True)
            final, temporary = root / identity, root / (".upload-" + identity)
            if final.is_file():
                if final.read_bytes() != raw:
                    raise ValueError("Immutable artifact content changed on retry")
            else:
                try:
                    with temporary.open("wb") as target:
                        target.write(raw)
                        target.flush()
                        os.fsync(target.fileno())
                    os.replace(temporary, final)
                finally:
                    temporary.unlink(missing_ok=True)
            artifacts.append(
                {
                    **{k: v for k, v in artifact.items() if k != "markdown"},
                    "sha256": hashlib.sha256(raw).hexdigest(),
                    "size_bytes": len(raw),
                }
            )
        # Конфигурация и происхождение остаются в отчёте, содержимое — в постоянном томе.
        result["artifacts"] = artifacts
        return artifacts

    @activity.defn(name="review_execute_v1")
    def execute(self, request):
        job = self.repo.job_by_id(UUID(request["job_id"]))
        if "outcome" in job["report"]:
            return job["report"]["outcome"]
        purpose = job["parameters"]["purpose"]
        try:
            if not self.settings.analysis_url:
                raise ValueError("Analysis service is not configured")
            data = {
                "schema_version": 1,
                "operation": purpose,
                "job_id": str(job["id"]),
                "program_id": str(job["program_id"]),
                "version_id": str(job["input_version_id"]),
                **job["parameters"]["input"],
            }
            data = (
                (PrepareRequest if purpose == "prepare" else CheckRequest)
                .model_validate(data)
                .model_dump(mode="json")
            )
            with httpx.Client(
                timeout=self.settings.analysis_timeout_seconds, trust_env=False
            ) as client:
                with client.stream(
                    "POST",
                    self.settings.analysis_url.rstrip("/")
                    + ("/analyze" if purpose == "prepare" else "/check"),
                    json=data,
                    headers={"Idempotency-Key": str(job["id"])},
                ) as response:
                    if response.status_code == 429 or response.status_code >= 500:
                        raise ApplicationError("ML временно недоступен", type="AnalysisTemporary")
                    response.raise_for_status()
                    chunks, size = [], 0
                    for chunk in response.iter_bytes():
                        size += len(chunk)
                        if size > self.settings.analysis_max_response_bytes:
                            raise ValueError("Analysis response exceeds configured limit")
                        chunks.append(chunk)
                    raw = json.loads(b"".join(chunks))
            artifacts = []
            if purpose == "prepare":
                parsed = AnalysisV1.model_validate(raw)
                documents = job["parameters"]["input"]["documents"]
                expected = {UUID(item["document_id"]) for item in documents}
                if set(parsed.candidate.document_ids) != expected:
                    raise ValueError("Analysis changed document membership")
                types = {item["parsed"]["meta"]["doc_type"] for item in documents}
                if not set(parsed.ruleset.required_document_types).issubset(types):
                    raise ValueError("Required documents are absent")
                result = map_analysis_ids(
                    parsed, job["id"], job["parameters"]["input"]["base_snapshot"]
                )
                for issue in result["issues"]:
                    for variant in issue["variants"]:
                        apply_changes(result["candidate"], variant["changes"])
                artifacts = self.save_artifacts(result)
            else:
                result = CheckResult.model_validate(raw).model_dump(mode="json")
                snapshot = SnapshotV2.model_validate(job["parameters"]["input"]["snapshot"])
                refs = {str(item.id) for item in snapshot.source_refs}
                for issue in result["issues"]:
                    if not set(issue["source_ref_ids"]).issubset(refs):
                        raise ValueError("Check references another version's sources")
                    issue["id"] = str(uuid5(job["id"], issue["id"]))
                    for variant in issue["variants"]:
                        variant["id"] = str(uuid5(job["id"], variant["id"]))
                        apply_changes(snapshot, variant["changes"])
            raw_hash = digest(raw)
            raw_record = {**raw}
            if purpose == "prepare":
                raw_record["artifacts"] = [
                    {
                        **{k: v for k, v in item.items() if k != "markdown"},
                        "stored_artifact_id": artifact["id"],
                        "sha256": artifact["sha256"],
                    }
                    for item, artifact in zip(raw.get("artifacts", []), artifacts, strict=True)
                ]
            return self.repo.publish(job["id"], result, artifacts, raw_record, raw_hash)
        except httpx.TransportError as error:
            raise ApplicationError("Нет соединения с ML", type="AnalysisTemporary") from error
        except (httpx.HTTPStatusError, ValidationError, ValueError, DomainError) as error:
            self.repo.fail(
                job["id"],
                job["workflow_id"],
                "analysis_invalid_result",
                "ML-результат не соответствует контракту или обязательным требованиям",
            )
            activity.logger.warning("Результат анализа отклонён: %s", type(error).__name__)
            return "failed"

    @activity.defn(name="review_failed_v1")
    def failed(self, request):
        job = self.repo.job_by_id(UUID(request["job_id"]))
        self.repo.fail(
            job["id"],
            job["workflow_id"],
            "analysis_unavailable",
            "Анализ не завершился после повторных попыток; исходные данные сохранены",
        )
