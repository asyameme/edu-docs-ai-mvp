"""Проекции версии графа в документные JSON для фазы graph_validation.

Валидатор (``worker.validation.validator.run_validation``) работает по
документным записям ``$.disciplines[*]``, ``$.curriculum_items[*]`` и т.п., а не
по графу. После правок атрибутов исходные JSON пакета устарели бы, поэтому
проекция строится так: база — сохранённые при импорте разобранные документы
(``metadata.parsed_documents``), поверх их записей — текущие атрибуты сущностей
и связей снимка, сопоставленные по координатам источников. Так прогоны правил
после правки видят именно новую версию.

Соответствие видов сущностей группам записей — обратное к ``GROUPS`` из
``app/document_adapter.py``; атрибуты сущности при импорте — это запись без
``source_ref``, поэтому запись восстанавливается точно.
"""

from __future__ import annotations

import copy
import json
from collections.abc import Mapping
from typing import Any

from app.domain import SnapshotV2

GROUP_BY_KIND = {
    "discipline": "disciplines",
    "competency": "competencies",
    "indicator": "indicators",
    "learning_outcome": "learning_outcomes",
    "assessment_tool": "assessment_tools",
    "curriculum_item": "curriculum_items",
    "course_content": "course_content",
    "score_scale": "score_scale",
    "document_section": "document_sections",
    "semester_load": "semester_workload",
    "practice_stage": "practice_stages",
    "professional_role": "professional_roles",
    "program": "program",
}

# Связи, поднявшиеся из relations-записей документов, несут src/dst-ключи в
# атрибутах; связи из matrix_cells/indicator_outcome_links/role_competency_links
# в records-списки документов не входят и в проекции не синхронизируются.
RELATION_MARKER = "src_entity"


class GraphProjectionError(ValueError):
    """Проекцию нельзя построить: в снимке нет разобранных документов."""


def _canonical(value: Any) -> str:
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"))


def _replace_in_list(records: list[Any], targets: list[tuple[str, Mapping[str, Any]]]) -> None:
    pending = list(targets)
    for index, record in enumerate(records):
        if not isinstance(record, Mapping) or "source_ref" not in record or not pending:
            continue
        locator = _canonical(record["source_ref"])
        for position, (target_locator, attributes) in enumerate(pending):
            if target_locator != locator:
                continue
            if RELATION_MARKER in attributes and (
                record.get("src_key") != attributes.get("src_key")
                or record.get("dst_key") != attributes.get("dst_key")
                or record.get("kind") != attributes.get("kind")
            ):
                continue
            records[index] = {**attributes, "source_ref": record["source_ref"]}
            pending.pop(position)
            break


def document_projections(snapshot: SnapshotV2) -> list[dict[str, Any]]:
    """Документные JSON, отражающие текущее состояние версии.

    Возвращает проекции в порядке ``document_ids`` снимка. Записи, не
    поднятые в граф (скалярные поля, диагностика), остаются от базы.
    При нескольких наблюдениях одной записи применяется последнее — как в
    разрешении конфликтов, но без выбора за пользователя.
    """

    parsed = snapshot.metadata.get("parsed_documents")
    if not isinstance(parsed, Mapping) or not parsed:
        raise GraphProjectionError(
            "В метаданных снимка нет parsed_documents: проекция документов недоступна"
        )
    documents = copy.deepcopy({str(doc_id): value for doc_id, value in parsed.items()})
    refs = {ref.id: ref for ref in snapshot.source_refs}

    by_document: dict[str, dict[str, list[tuple[str, Mapping[str, Any]]]]] = {}

    def register(document_id: Any, locator: str, group: str, attributes: Mapping[str, Any]) -> None:
        grouped = by_document.setdefault(str(document_id), {})
        grouped.setdefault(group, []).append((locator, attributes))

    for entity in snapshot.entities:
        group = GROUP_BY_KIND.get(entity.kind)
        if group is None:
            continue
        for ref_id in entity.source_ref_ids:
            ref = refs.get(ref_id)
            if ref is not None:
                register(ref.document_id, ref.locator, group, entity.attributes)
    for relation in snapshot.relations:
        if RELATION_MARKER not in relation.attributes:
            continue
        for ref_id in relation.source_ref_ids:
            ref = refs.get(ref_id)
            if ref is not None:
                register(ref.document_id, ref.locator, "relations", relation.attributes)

    for doc_id in snapshot.document_ids:
        document = documents.get(str(doc_id))
        if not isinstance(document, dict):
            continue
        for group, targets in by_document.get(str(doc_id), {}).items():
            if group == "program":
                record = document.get("program")
                if isinstance(record, Mapping) and "source_ref" in record and targets:
                    document["program"] = {**targets[-1][1], "source_ref": record["source_ref"]}
                continue
            records = document.get(group)
            if isinstance(records, list):
                _replace_in_list(records, targets)

    return [documents[str(doc_id)] for doc_id in snapshot.document_ids if str(doc_id) in documents]
