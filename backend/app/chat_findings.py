"""Компактная сводка замечаний и проверенный ответ при исчерпании бюджета."""

import hashlib
import json
import re
import unicodedata
from collections import Counter
from typing import Literal
from uuid import UUID

from pydantic import BaseModel

R11_TITLES = {
    "rpd_section_2_missing": "Не извлечено содержание раздела 2",
    "rpd_section_3_evidence_missing": "Не найдены связанные данные раздела 3",
    "rpd_multiple_disciplines_unsupported": "Неоднозначна привязка содержания к дисциплине",
    "rpd_outcome_id_ambiguous": "Результат обучения не удалось однозначно определить",
    "rpd_outcome_competency_link_missing": "Не найдена связь результата с компетенцией",
    "rpd_semantic_model_unavailable": "Смысловую проверку не удалось выполнить",
    "rpd_semantic_response_invalid": "Ответ смысловой проверки требует проверки",
    "rpd_semantic_result_missing_or_duplicate": "Модель не вернула однозначный результат проверки",
    "rpd_semantic_unexpected_outcome_ids": "Ответ содержит неизвестные результаты обучения",
    "rpd_semantic_alignment_gap": "Возможный пробел в содержании или оценивании",
    "rpd_semantic_alignment_uncertain": "Связь требует экспертной оценки",
    "rpd_semantic_evidence_missing": "Не хватает подтверждений для вывода",
}


class FindingDocument(BaseModel):
    id: UUID
    name: str


class FindingGroup(BaseModel):
    id: str
    rule_code: str
    category: str
    severity: str
    title: str
    message: str
    record_count: int
    status_counts: dict[str, int]
    documents: list[FindingDocument]
    document_count: int
    issue_ids: list[UUID]
    source_ref_ids: list[UUID]
    truncated: bool = False


class FindingsPage(BaseModel):
    scope: Literal["automatic_review"] = "automatic_review"
    version_id: UUID
    status: Literal["all", "attention"] = "all"
    total_groups: int
    total_records: int
    groups: list[FindingGroup]
    has_more: bool
    next_cursor: str | None = None
    truncated: bool = False


def _short(text, limit):
    text = str(text or "")
    return text if len(text) <= limit else text[:limit] + "…[сокращено]"


def build_findings_page(
    rows, *, version_id, documents, references, limit=10, after=None, status="all"
):
    """Группируем полный актуальный набор до ограничения страницы, как в UI."""
    if status not in {"all", "attention"} or not 1 <= limit <= 20:
        raise ValueError("Некорректные параметры сводки")
    grouped = {}
    identities = []
    for row in rows:
        state = row.get("effective_status", row.get("status", "open"))
        if status == "attention" and state not in {"open", "needs_review"}:
            continue
        message = row.get("message") or "Описание замечания отсутствует"
        title = R11_TITLES.get(row["type"], message) if row["rule_code"] == "R-11" else message
        title = re.sub(r"\s+", " ", unicodedata.normalize("NFKC", title)).strip()
        severity = "error" if row["severity"] == "blocking" else "warning"
        key = json.dumps(
            ["validation", severity, row["rule_code"], row["type"], title],
            ensure_ascii=False,
            separators=(",", ":"),
        )
        identities.append((str(row["id"]), state))
        group = grouped.setdefault(key, {"rows": [], "title": title, "severity": severity})
        group["rows"].append((row, state))
    keys = sorted(
        grouped,
        key=lambda k: (
            grouped[k]["severity"] != "error",
            re.sub(r"\d+", lambda m: m[0].zfill(8), grouped[k]["rows"][0][0]["rule_code"]),
            k,
        ),
    )
    scope = hashlib.sha256(
        json.dumps([str(version_id), status, sorted(identities)], ensure_ascii=False).encode()
    ).hexdigest()[:24]
    start = 0
    if after:
        try:
            prefix, value = after.split(":")
            start = int(value)
            if prefix != scope or not 0 <= start < len(keys):
                raise ValueError
        except (ValueError, AttributeError):
            raise ValueError("Устаревший или неверный курсор: запросите сводку заново") from None
    result = []
    size = 0
    for key in keys[start : start + limit]:
        group = grouped[key]
        members = group["rows"]
        first = members[0][0]
        refs = sorted({str(ref) for row, _ in members for ref in row.get("source_ref_ids", [])})
        doc_ids = sorted(
            {str(d) for row, _ in members for d in row.get("source_document_ids", [])}
            | {references[r] for r in refs if r in references}
        )
        message = _short(first.get("message"), 1800)
        item = FindingGroup(
            id=hashlib.sha256(key.encode()).hexdigest()[:24],
            rule_code=first["rule_code"],
            category=first["type"],
            severity=group["severity"],
            title=_short(group["title"], 1800),
            message=message,
            record_count=len(members),
            status_counts=dict(Counter(state for _, state in members)),
            documents=[
                FindingDocument(id=d, name=_short(documents.get(d, "Название недоступно"), 255))
                for d in doc_ids[:10]
            ],
            document_count=len(doc_ids),
            issue_ids=[row["id"] for row, _ in members[:10]],
            source_ref_ids=refs[:5],
            truncated=(
                len(doc_ids) > 10
                or len(refs) > 5
                or len(members) > 10
                or len(str(first.get("message") or "")) > 1800
                or len(group["title"]) > 1800
            ),
        )
        item_size = len(item.model_dump_json().encode())
        if result and size + item_size > 48_000:
            break
        result.append(item)
        size += item_size
    end = start + len(result)
    has_more = end < len(keys)
    return FindingsPage(
        version_id=version_id,
        status=status,
        total_groups=len(keys),
        total_records=len(identities),
        groups=result,
        has_more=has_more,
        next_cursor=f"{scope}:{end}" if has_more else None,
        truncated=has_more or any(g.truncated for g in result),
    )


def findings_limit_answer(pages, reason):
    """Не выдаём незавершённый анализ за исправление или полный ответ модели."""
    cause = {
        "tools": "количества действий",
        "tokens": "объёма токенов",
        "requests": "числа обращений к модели",
    }.get(reason, "бюджета запроса")
    lines = [f"Подробный анализ не завершён: достигнуто ограничение {cause}."]
    groups = {}
    for page in pages:
        for group in page.groups:
            groups[group.id] = group
    if groups:
        lines.append("Уже полученные замечания (это частичная сводка):")
        for number, group in enumerate(groups.values(), 1):
            names = "; ".join(d.name for d in group.documents[:3])
            states = {
                "open": "требует внимания",
                "needs_review": "требует проверки",
                "accepted": "предупреждение принято",
                "resolved": "решение выбрано",
            }
            label = ", ".join(f"{states.get(k, k)}: {v}" for k, v in group.status_counts.items())
            title = group.title.replace("hours_total", "общий объём часов").replace(
                "credit_units", "зачётные единицы").replace("RPD", "РПД")
            line = f"{number}. {title} ({label})" + (f" Документы: {names}." if names else "")
            if len("\n".join(lines)) + len(line) > 10_000:
                break
            lines.append(line)
        lines.append(
            "Список может быть неполным. Выберите замечание для продолжения; "
            "полный список доступен в результатах проверки выбранной версии."
        )
    else:
        lines.append(
            "До остановки не удалось собрать данные для частичного ответа. "
            "Уточните задачу или выберите "
            "конкретную проблему в результатах проверки."
        )
    return {"kind": "answer", "message": "\n".join(lines), "sources": []}


def limit_reason(error):
    """Различаем пределы библиотеки, не показывая внутреннее исключение пользователю."""
    text = str(error).lower()
    if "tool_calls_limit" in text:
        return "tools"
    if "token" in text:
        return "tokens"
    if "request_limit" in text:
        return "requests"
    return "budget"
