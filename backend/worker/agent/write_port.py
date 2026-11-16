"""Контур применения предложений: контракт, который реализует бекенд.

Логика правки (проверка предложения, новый снимок, вход генерации) — чистые
функции ``proposal_apply``; всё, что требует базы и Temporal, — за этим
протоколом. Каждая правка применяется отдельной версией (``VersionCreate`` с
``expected_version_id`` предыдущей — конкурентная правка падает на
оптимистичной блокировке и уходит в разрешение конфликта), после КАЖДОЙ правки
прогоняются нормы и находки прикладываются к результату. Порядок и транзакции —
на стороне реализации; сам протокол идемпотентен по (version_id, предложение).
"""

from typing import Literal, Protocol
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from .contracts import ChangeProposal, ProposedAttributeChange, ValidationFinding


class AppliedChange(BaseModel):
    """Результат одной применённой правки: новая версия и её проверка норм."""

    model_config = ConfigDict(extra="forbid")

    change: ProposedAttributeChange
    new_version_id: UUID
    findings: list[ValidationFinding] = Field(default_factory=list)


class ChangeApplicationResult(BaseModel):
    """Итог применения предложения: все правки или обоснованный отказ."""

    model_config = ConfigDict(extra="forbid")

    status: Literal["applied", "rejected"]
    base_version_id: UUID
    applied: list[AppliedChange] = Field(default_factory=list)
    rejection_reason: str | None = None
    touched_document_ids: list[UUID] = Field(default_factory=list)


class ChatWritePort(Protocol):
    """Применение предложений агента; реализация живёт в бекенд-воркере."""

    async def apply_change_proposal(
        self,
        *,
        program_id: UUID,
        version_id: UUID,
        proposal: ChangeProposal,
        signature: str,
    ) -> ChangeApplicationResult: ...
