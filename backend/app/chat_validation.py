"""Инструменты чата для запуска и чтения проверок через review API."""

import asyncio
import hashlib
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, ValidationError
from pydantic_ai import Agent, ModelRetry, RunContext
from pydantic_core import to_json

from app.domain import SnapshotV2
from app.errors import DomainError
from app.repository import Repository
from app.review_contract import CheckCreate, CheckResult
from app.review_projection import project_legacy_review_result
from app.review_repository import ReviewRepository
from worker.agent.contracts import ChatDeps

MAX_CHAT_TOOL_PAYLOAD_BYTES = 64 * 1024
MAX_CHAT_CHECK_COVERAGE = 30
MAX_CHAT_CHECK_ISSUES = 12


def _stable_check_request_id(run_id: str, tool_call_id: str | None) -> str:
    """Строит ключ вызова проверки, устойчивый к повторам и уникальный между ходами."""

    if not run_id:
        raise RuntimeError("Для вызова проверки отсутствует идентификатор хода агента")
    return f"{run_id}:{tool_call_id or 'request'}"


def _bounded_text(value: str | None, max_bytes: int) -> tuple[str | None, bool]:
    """Ограничивает UTF-8 строку по байтам и сообщает об усечении."""

    if value is None:
        return None, False
    encoded = value.encode("utf-8")
    if len(encoded) <= max_bytes:
        return value, False
    suffix = "…".encode()
    prefix = encoded[: max(0, max_bytes - len(suffix))].decode("utf-8", errors="ignore")
    return prefix + "…", True


def _fit_chat_check_result(result: "ChatCheckResult") -> "ChatCheckResult":
    """Гарантирует лимит размера всего JSON-ответа инструмента."""

    while len(to_json(result)) > MAX_CHAT_TOOL_PAYLOAD_BYTES:
        result.truncated = True
        if result.issues:
            result.issues.pop()
        elif result.coverage:
            result.coverage.pop()
        elif result.message:
            result.message, _ = _bounded_text(result.message, 256)
        else:
            raise RuntimeError("Не удалось ограничить размер результата проверки чата")
    if result.truncated and result.all_rules_passed is True:
        result.all_rules_passed = False
    return result


class ChatCheckCoverage(BaseModel):
    model_config = ConfigDict(extra="forbid")

    rule_code: str
    status: Literal["passed", "failed", "skipped", "not_applicable"]
    reason: str | None = None


class ChatCheckIssue(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: UUID
    rule_code: str
    type: str
    severity: Literal["warning", "blocking"]
    status: Literal["open", "needs_review"]
    message: str
    requires_decision: bool
    source_ref_ids: list[UUID]


class ChatCheckResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    check_job_id: UUID
    version_id: UUID
    input_version_id: UUID
    output_version_id: UUID | None = None
    status: Literal["pending", "completed", "partial", "failed"]
    message: str | None = None
    coverage: list[ChatCheckCoverage]
    issues: list[ChatCheckIssue]
    total_coverage: int
    total_issues: int
    truncated: bool
    all_rules_passed: bool | None


class ChatValidationTools:
    """Связывает инструменты агента с существующим хранилищем review checks."""

    def __init__(self, pool):
        self.pool = pool

    def _review_for_version(self, program_id: UUID, version_id: UUID):
        row = Repository(self.pool).version(program_id, version_id)
        snapshot = SnapshotV2.model_validate(row["snapshot"])
        if snapshot.review is None:
            raise DomainError(409, "У выбранной версии нет подготовки для проверки")
        if not snapshot.review.graph_ready:
            raise DomainError(409, "Нельзя проверить версию до сборки графа")
        return snapshot.review.review_id

    def request_check(self, program_id: UUID, version_id: UUID, request_id: str) -> ChatCheckResult:
        """Создаёт идемпотентную job для конкретного вызова инструмента агента."""

        if not request_id:
            raise ValueError("Для проверки чата требуется стабильный идентификатор вызова")
        review_id = self._review_for_version(program_id, version_id)
        request_hash = hashlib.sha256(request_id.encode("utf-8")).hexdigest()
        job_id = ReviewRepository(self.pool).create(
            program_id,
            f"chat-agent-check:{program_id}:{version_id}:{request_hash}",
            CheckCreate(version_id=version_id),
            purpose="check",
            review_id=review_id,
            allow_historical=True,
        )
        return self.read_check(program_id, version_id, job_id)

    def read_check(self, program_id: UUID, version_id: UUID, check_job_id: UUID) -> ChatCheckResult:
        review_id = self._review_for_version(program_id, version_id)
        job = ReviewRepository(self.pool).job(program_id, check_job_id)
        if (
            job["parameters"].get("purpose") != "check"
            or job["parameters"].get("review_id") != str(review_id)
            or UUID(str(job["input_version_id"])) != version_id
        ):
            raise DomainError(404, "Проверка не найдена в выбранной версии")

        report = job["report"]
        outcome = report.get("outcome")
        status = outcome if outcome in {"completed", "partial", "failed"} else "pending"
        message = None
        coverage: list[ChatCheckCoverage] = []
        issues: list[ChatCheckIssue] = []
        total_coverage = 0
        total_issues = 0
        truncated = False
        all_rules_passed: bool | None = None

        if status in {"completed", "partial"}:
            raw_result = report.get("result")
            if not isinstance(raw_result, dict) and status == "completed":
                raise DomainError(503, "Результат проверки временно недоступен")
            if isinstance(raw_result, dict):
                try:
                    result = CheckResult.model_validate(project_legacy_review_result(raw_result))
                except ValidationError as error:
                    raise DomainError(503, "Сохранённый результат проверки повреждён") from error
                total_coverage = len(result.coverage)
                total_issues = len(result.issues)
                coverage_truncated = total_coverage > MAX_CHAT_CHECK_COVERAGE
                issues_truncated = total_issues > MAX_CHAT_CHECK_ISSUES
                truncated = coverage_truncated or issues_truncated
                for item in result.coverage[:MAX_CHAT_CHECK_COVERAGE]:
                    rule_code, code_cut = _bounded_text(item.rule_code, 160)
                    reason, reason_cut = _bounded_text(item.reason, 320)
                    truncated |= code_cut or reason_cut
                    coverage.append(
                        ChatCheckCoverage(
                            rule_code=rule_code,
                            status=item.status,
                            reason=reason,
                        )
                    )
                for item in result.issues[:MAX_CHAT_CHECK_ISSUES]:
                    rule_code, code_cut = _bounded_text(item.rule_code, 160)
                    issue_type, type_cut = _bounded_text(item.type, 120)
                    message, message_cut = _bounded_text(item.message, 640)
                    source_ref_ids = item.source_ref_ids[:5]
                    truncated |= (
                        code_cut
                        or type_cut
                        or message_cut
                        or len(source_ref_ids) < len(item.source_ref_ids)
                    )
                    issues.append(
                        ChatCheckIssue(
                            id=item.id,
                            rule_code=rule_code,
                            type=issue_type,
                            severity=item.severity,
                            status=item.status,
                            message=message,
                            requires_decision=item.requires_decision,
                            source_ref_ids=source_ref_ids,
                        )
                    )
                if status == "completed":
                    all_rules_passed = (
                        bool(result.coverage)
                        and not result.issues
                        and all(item.status == "passed" for item in result.coverage)
                    )
            elif status == "partial":
                message = "Проверка выполнена частично; сохранённый результат недоступен."
        elif status == "failed":
            message = "Проверка завершилась ошибкой; подробности доступны в review API."
        else:
            message = "Проверка поставлена в очередь или ещё выполняется."

        message, message_cut = _bounded_text(message, 512)
        truncated |= message_cut
        return _fit_chat_check_result(
            ChatCheckResult(
                check_job_id=check_job_id,
                version_id=version_id,
                input_version_id=UUID(str(job["input_version_id"])),
                output_version_id=(
                    UUID(str(job["output_version_id"])) if job.get("output_version_id") else None
                ),
                status=status,
                message=message,
                coverage=coverage,
                issues=issues,
                total_coverage=total_coverage,
                total_issues=total_issues,
                truncated=truncated,
                all_rules_passed=(
                    all_rules_passed and not truncated if all_rules_passed is not None else None
                ),
            )
        )


_chat_validation_tools: ChatValidationTools | None = None


def configure_chat_validation_tools(pool) -> None:
    """Передаёт общий PostgreSQL pool backend-инструментам до старта Worker."""

    global _chat_validation_tools
    if _chat_validation_tools is not None and _chat_validation_tools.pool is not pool:
        raise RuntimeError("Инструменты проверки чата уже подключены к другому pool")
    _chat_validation_tools = ChatValidationTools(pool)


def get_chat_validation_tools() -> ChatValidationTools:
    if _chat_validation_tools is None:
        raise RuntimeError("Инструменты проверки чата не настроены Worker")
    return _chat_validation_tools


def register_chat_validation_tools(agent: Agent) -> None:
    """Добавляет backend-инструменты существующему агенту, не меняя ML-модуль."""

    @agent.tool
    async def request_program_check(ctx: RunContext[ChatDeps]) -> ChatCheckResult:
        """Запускает проверку правил для версии программы, закреплённой за чатом.

        Используй инструмент по явной просьбе проверить или перепроверить программу.
        Проверка асинхронная. Если статус pending, сообщи об этом и запомни
        check_job_id для следующего вызова read_program_check. Статус completed
        означает только завершение вычисления; он не подтверждает прохождение правил.
        Не делай вывод о прохождении до read_program_check и поля all_rules_passed=true.
        """

        deps = ctx.deps
        request_id = _stable_check_request_id(ctx.run_id, ctx.tool_call_id)
        return await asyncio.to_thread(
            get_chat_validation_tools().request_check,
            deps.program_id,
            deps.version_id,
            request_id,
        )

    @agent.tool
    async def read_program_check(ctx: RunContext[ChatDeps], check_job_id: UUID) -> ChatCheckResult:
        """Читает результат проверки только версии, закреплённой за этим чатом.

        При pending сообщи, что задача ещё выполняется. При partial или failed
        объясни, что проверка не завершена полностью или завершилась ошибкой.
        Даже при completed говори, что все правила пройдены, только если
        all_rules_passed=true; иначе перечисли статусы покрытия и замечания.
        При truncated=true явно сообщи, что показана только часть результата.
        """

        deps = ctx.deps
        try:
            return await asyncio.to_thread(
                get_chat_validation_tools().read_check,
                deps.program_id,
                deps.version_id,
                check_job_id,
            )
        except DomainError as error:
            if error.status == 404:
                raise ModelRetry(
                    "Эта проверка не найдена для версии программы, выбранной в чате. "
                    "Используй check_job_id, полученный из request_program_check."
                ) from error
            raise
