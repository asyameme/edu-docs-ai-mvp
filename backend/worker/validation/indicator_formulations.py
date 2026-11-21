"""Детерминированная сверка формулировок одинаковых индикаторов в документах."""
from __future__ import annotations

import json
import unicodedata
from collections import defaultdict
from dataclasses import dataclass
from typing import Any, Mapping, Sequence

if __package__:
    from .text_normalization import normalize_variant_formulation_text
else:  # Поддерживаем импорт при прямом запуске validator.py.
    from text_normalization import normalize_variant_formulation_text


@dataclass(frozen=True)
class IndicatorDocument:
    label: str
    document_type: str
    program_id: str | None
    indicators: Sequence[Mapping[str, Any]]


@dataclass(frozen=True)
class IndicatorOccurrence:
    occurrence_id: str
    document_key: int
    document_label: str
    document_type: str
    code: str
    text: str
    source_ref: Mapping[str, Any]


def _normalise_code(value: str) -> str:
    return unicodedata.normalize("NFKC", value).strip().casefold()


def _normalise_text(value: str | None) -> str:
    if value is None:
        return ""
    return normalize_variant_formulation_text(value)


def _source_refs(occurrences: Sequence[IndicatorOccurrence]) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    seen: set[str] = set()
    for occurrence in occurrences:
        ref = dict(occurrence.source_ref)
        key = json.dumps(ref, ensure_ascii=False, sort_keys=True, default=str)
        if key not in seen:
            seen.add(key)
            result.append(ref)
    return result


def _finding(
    code: str,
    message: str,
    occurrences: Sequence[IndicatorOccurrence],
    *,
    extra: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    evidence: dict[str, Any] = {
        "indicator_code": occurrences[0].code if occurrences else None,
        "source_refs": _source_refs(occurrences),
    }
    if extra:
        evidence.update(extra)
    return {"code": code, "status": "needs_review", "message": message, "evidence": evidence}


def _group_variants(occurrences: Sequence[IndicatorOccurrence]) -> list[dict[str, Any]]:
    """Возвращает исходные варианты; эталонный текст не назначается."""
    if len({item.text for item in occurrences}) < 2:
        return []
    return [
        {
            "document": item.document_label,
            "code": item.code,
            "text": item.text,
            "source_ref": dict(item.source_ref),
        }
        for item in occurrences
    ]


def _collect_occurrences(
    documents: Sequence[IndicatorDocument],
) -> tuple[dict[tuple[str, str], list[IndicatorOccurrence]], int, list[dict[str, Any]]]:
    groups: dict[tuple[str, str], list[IndicatorOccurrence]] = defaultdict(list)
    checks = 0
    findings: list[dict[str, Any]] = []
    for document_key, document in enumerate(documents):
        package_key = document.program_id or "__single_validation_package__"
        for index, row in enumerate(document.indicators, start=1):
            code = row.get("code")
            text = row.get("text")
            source_ref = row.get("_source_ref")
            source_ref = dict(source_ref) if isinstance(source_ref, Mapping) else {"file": document.label}
            if not isinstance(code, str) or not code.strip() or not isinstance(text, str) or not text.strip():
                checks += 1
                findings.append(_finding(
                    "indicator_data_missing",
                    f"Нельзя сравнить индикатор в {document.label}: отсутствует корректный код или формулировка.",
                    [IndicatorOccurrence(
                        f"D{document_key + 1}-I{index}", document_key, document.label,
                        document.document_type, str(code or ""), str(text or ""), source_ref,
                    )],
                ))
                continue

            occurrence = IndicatorOccurrence(
                occurrence_id=f"D{document_key + 1}-I{index}",
                document_key=document_key,
                document_label=document.label,
                document_type=document.document_type,
                code=code.strip(),
                text=text.strip(),
                source_ref=source_ref,
            )
            groups[(package_key, _normalise_code(code))].append(occurrence)
    return groups, checks, findings


def evaluate_indicator_formulations(
    *,
    documents: Sequence[IndicatorDocument],
) -> tuple[int, list[dict[str, Any]]]:
    """Группирует разные варианты одного кода для решения пользователем."""
    grouped, checks, findings = _collect_occurrences(documents)
    for _, occurrences in sorted(grouped.items(), key=lambda item: item[0]):
        distinct_documents = {item.document_key for item in occurrences}
        if len(distinct_documents) < 2:
            continue
        checks += 1

        # Различия оформления (регистр, пробелы, краевая пунктуация) не требуют выбора.
        if len({_normalise_text(item.text) for item in occurrences}) <= 1:
            continue

        variants = _group_variants(occurrences)
        findings.append(_finding(
            "indicator_formulation_review",
            f"Формулировки индикатора {occurrences[0].code} различаются после нормализации. Выберите вариант для документов.",
            occurrences,
            extra={
                "variants": variants,
                "requires_user_confirmation": bool(variants),
                "auto_select": False,
            },
        ))
    return checks, findings
