"""Ограниченный HTTP-обмен с анализатором и запись долговечного результата."""

import hashlib
import json
import os
from uuid import UUID, uuid4, uuid5

import httpx
from pydantic import ValidationError
from temporalio import activity
from temporalio.exceptions import ApplicationError

from app.document_adapter import to_snapshot_v2
from app.document_analysis import analyze_documents
from app.document_reconciliation import validate_document_mutation_variants
from app.domain import SnapshotV2
from app.errors import DomainError
from app.review_contract import (
    AnalysisV1,
    AnalysisV2,
    CheckRequest,
    CheckResult,
    PrepareRequest,
    PrepareRequestV2,
)
from app.review_logic import apply_changes, digest, map_analysis_ids, map_analysis_v2_ids
from app.review_projection import validator_result_for_contract
from app.review_repository import ReviewRepository
from app.validation_bridge import (
    project_snapshot_documents,
    run_local_check,
    validation_implementation_revision,
    validation_ruleset,
)
from worker.validation.validator import Document, load_rules


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
            final = root / identity
            temporary = root / (".upload-" + identity + "-" + uuid4().hex)
            if final.is_file():
                if final.read_bytes() != raw:
                    raise ValueError("Immutable artifact content changed on retry")
            else:
                try:
                    with temporary.open("xb") as target:
                        target.write(raw)
                        target.flush()
                        os.fsync(target.fileno())
                    # Жёсткая ссылка создаёт итоговый путь только при его отсутствии.
                    # Конкурирующая попытка не может перезаписать уже опубликованные байты.
                    try:
                        os.link(temporary, final)
                    except FileExistsError:
                        if final.read_bytes() != raw:
                            raise ValueError(
                                "Immutable artifact content changed on retry"
                            ) from None
                finally:
                    temporary.unlink(missing_ok=True)
            directory_fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
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
        is_local = job["parameters"].get("executor", "http") == "local"
        try:
            if is_local:
                input_data = job["parameters"]["input"]
                rules = job["parameters"].get("local_rules") or load_rules()
                expected_implementation = job["parameters"].get("implementation_revision")
                if expected_implementation != validation_implementation_revision():
                    raise ValueError(
                        "Ревизия локального валидатора изменилась после создания задания"
                    )
                inputs = [
                    Document(item["parsed"], str(item["document_id"]), str(item["document_id"]))
                    for item in input_data["documents"]
                ]
                ruleset = validation_ruleset(rules, inputs)
                if purpose == "prepare" and job["parameters"].get("schema_version", 1) == 2:
                    parsed_request = PrepareRequestV2.model_validate({
                        "schema_version": 2, "operation": "prepare",
                        "job_id": job["id"], "program_id": job["program_id"],
                        "version_id": job["input_version_id"], **input_data,
                    })
                    raw = analyze_documents(
                        [item.model_dump(mode="json") for item in parsed_request.documents],
                        job["id"], config=rules,
                    )
                elif purpose == "prepare":
                    files = [
                        {"document_id": item["document_id"], "output": item["parsed"]}
                        for item in input_data["documents"]
                    ]
                    candidate, _ = to_snapshot_v2(job["program_id"], files)
                    result = run_local_check(
                        input_data["documents"],
                        SnapshotV2.model_validate(candidate),
                        job["id"],
                        config=rules,
                        ruleset=ruleset,
                    )
                    raw = {**result, "candidate": candidate, "artifacts": []}
                else:
                    snapshot = SnapshotV2.model_validate(input_data["snapshot"])
                    documents = project_snapshot_documents(input_data["documents"], snapshot)
                    raw = run_local_check(
                        documents,
                        snapshot,
                        job["id"],
                        config=input_data["ruleset"]["config"],
                        ruleset=input_data["ruleset"],
                    )
            else:
                if not self.settings.analysis_url:
                    raise ValueError("Analysis service is not configured")
                input_data = job["parameters"]["input"]
                if purpose == "check" and job["parameters"].get("project_check_documents"):
                    snapshot = SnapshotV2.model_validate(input_data["snapshot"])
                    input_data = {
                        **input_data,
                        "documents": project_snapshot_documents(
                            input_data["documents"], snapshot
                        ),
                    }
                data = {
                    "schema_version": job["parameters"].get("schema_version", 1),
                    "operation": purpose,
                    "job_id": str(job["id"]),
                    "program_id": str(job["program_id"]),
                    "version_id": str(job["input_version_id"]),
                    **input_data,
                }
                request_model = (
                    PrepareRequestV2
                    if purpose == "prepare" and data["schema_version"] == 2
                    else PrepareRequest
                    if purpose == "prepare"
                    else CheckRequest
                )
                data = request_model.model_validate(data).model_dump(mode="json")
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
                            raise ApplicationError(
                                "ML временно недоступен", type="AnalysisTemporary"
                            )
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
                documents = job["parameters"]["input"]["documents"]
                if job["parameters"].get("schema_version", 1) == 2:
                    response = validator_result_for_contract(raw) if is_local else raw
                    parsed = AnalysisV2.model_validate(response)
                    required_types = {
                        value.casefold() for value in parsed.ruleset.required_document_types
                    }
                    actual_types = {
                        item["parsed"]["meta"]["doc_type"].casefold() for item in documents
                    }
                    if not required_types.issubset(actual_types):
                        raise ValueError("Required documents are absent")
                    if any(issue.source_ref_ids for issue in parsed.issues):
                        raise ValueError("AnalysisV2 findings must identify source documents")
                    result = map_analysis_v2_ids(parsed, job["id"], documents)
                    original = {item["document_id"]: item["parsed"] for item in documents}
                    variants = [
                        [change.model_dump(mode="json") for change in variant.changes]
                        for issue in parsed.issues
                        for variant in issue.variants
                    ]
                    validate_document_mutation_variants(original, variants)
                    result["schema_version"] = 2
                    artifacts = self.save_artifacts(result)
                else:
                    response = validator_result_for_contract(raw) if is_local else raw
                    parsed = AnalysisV1.model_validate(response)
                    expected = {UUID(item["document_id"]) for item in documents}
                    if set(parsed.candidate.document_ids) != expected:
                        raise ValueError("Analysis changed document membership")
                    types = {item["parsed"]["meta"]["doc_type"].casefold() for item in documents}
                    required_types = {
                        value.casefold() for value in parsed.ruleset.required_document_types
                    }
                    if not required_types.issubset(types):
                        raise ValueError("Required documents are absent")
                    result = map_analysis_ids(
                        parsed, job["id"], job["parameters"]["input"]["base_snapshot"]
                    )
                    for issue in result["issues"]:
                        for variant in issue["variants"]:
                            apply_changes(result["candidate"], variant["changes"])
                    artifacts = self.save_artifacts(result)
            else:
                response = validator_result_for_contract(raw) if is_local else raw
                result = CheckResult.model_validate(response).model_dump(mode="json")
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
