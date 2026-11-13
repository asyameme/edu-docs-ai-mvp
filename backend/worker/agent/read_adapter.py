"""Адаптер чтения закреплённого снимка графовой версии программы."""

import asyncio
import json
from functools import wraps
from uuid import UUID

from pydantic_ai import ModelRetry
from pydantic_core import to_json

from app.config import get_settings
from app.domain import SnapshotV2
from app.errors import DomainError
from app.repository import Repository
from worker.agent.contracts import (
    EntityReadResult,
    EntitySearchHit,
    EntitySearchResult,
    GraphProjection,
    SourceFragment,
    ValidationFinding,
)
from worker.agent.read_port import configure_worker_read_port

MAX_TOOL_PAYLOAD_BYTES = 64 * 1024


def limit_tool_payload(function):
    """Проверяет весь сериализованный ответ до выхода из Temporal Activity."""

    @wraps(function)
    async def checked(*args, **kwargs):
        result = await function(*args, **kwargs)
        if len(to_json(result)) > MAX_TOOL_PAYLOAD_BYTES:
            raise ModelRetry(
                "Результат инструмента превышает 64 КиБ. Уменьшите limit/depth, "
                "уточните запрос или используйте другой инструмент чтения."
            )
        return result

    checked.__payload_limited__ = True
    return checked


def match_excerpt(text, query, context=160):
    """Возвращает исходный текст вокруг совпадения с учётом расширения casefold."""
    folded_query = query.casefold()
    position = text.casefold().find(folded_query)
    if position < 0:
        return None
    offset, start, end = 0, None, len(text)
    for index, character in enumerate(text):
        next_offset = offset + len(character.casefold())
        if start is None and next_offset > position:
            start = index
        if next_offset >= position + len(folded_query):
            end = index + 1
            break
        offset = next_offset
    left, right = max(0, (start or 0) - context), min(len(text), end + context)
    return ("…" if left else "") + text[left:right] + ("…" if right < len(text) else "")


def _bounded_value(value, budget=1800, depth=0):
    """Ограничивает размер вложенного значения из пользовательского графа."""
    if budget <= 0:
        return "[сокращено]"
    if isinstance(value, str):
        encoded = value.encode("utf-8")
        if len(encoded) <= budget:
            return value
        return encoded[: max(0, budget - 20)].decode("utf-8", errors="ignore") + "…[сокращено]"
    if isinstance(value, dict):
        if depth >= 3:
            return "[вложенность сокращена]"
        result = {}
        remaining = budget - 64
        for key, item in list(value.items())[:12]:
            if remaining <= 0:
                break
            safe_key = str(key)[:100]
            child_budget = min(remaining, max(96, budget // 12))
            safe_value = _bounded_value(item, child_budget, depth + 1)
            result[safe_key] = safe_value
            remaining -= len(json.dumps({safe_key: safe_value}, ensure_ascii=False).encode("utf-8"))
        return result
    if isinstance(value, list):
        if depth >= 3:
            return ["[вложенность сокращена]"]
        result = []
        remaining = budget - 32
        for item in value[:12]:
            if remaining <= 0:
                break
            safe_value = _bounded_value(item, min(remaining, max(96, budget // 12)), depth + 1)
            result.append(safe_value)
            remaining -= len(json.dumps(safe_value, ensure_ascii=False).encode("utf-8"))
        return result
    return value


def _bounded_entity(entity):
    return entity.model_copy(
        update={
            "attributes": _bounded_value(entity.attributes),
            "source_ref_ids": entity.source_ref_ids[:10],
        }
    )


def _bounded_relation(relation):
    return relation.model_copy(
        update={
            "attributes": _bounded_value(relation.attributes, budget=700),
            "source_ref_ids": relation.source_ref_ids[:10],
        }
    )


def _bounded_ref(ref):
    return ref.model_copy(
        update={
            "section": ref.section[:300] if ref.section else None,
            "locator": ref.locator[:300],
            "artifact_locator": ref.artifact_locator[:300] if ref.artifact_locator else None,
        }
    )


class PostgresChatReadPort:
    def __init__(self, pool):
        self.pool = pool

    def snapshot(self, program_id, version_id):
        row = Repository(self.pool).version(program_id, version_id)
        return SnapshotV2.model_validate(row["snapshot"])

    @limit_tool_payload
    async def search_entities(self, *, program_id, version_id, query, entity_kinds, limit):
        snapshot = await asyncio.to_thread(self.snapshot, program_id, version_id)
        kinds = set(entity_kinds or [])
        hits = []
        refs = {item.id: item for item in snapshot.source_refs}
        for entity in snapshot.entities:
            if kinds and entity.kind not in kinds:
                continue
            summary = json.dumps(entity.attributes, ensure_ascii=False, default=str)
            excerpt = match_excerpt(summary, query)
            if excerpt is None:
                continue
            bounded = _bounded_entity(entity)
            hits.append(
                EntitySearchHit(
                    entity=bounded,
                    summary=excerpt,
                    truncated=bounded != entity or len(entity.source_ref_ids) > 5,
                    source_refs=[
                        _bounded_ref(refs[item]) for item in entity.source_ref_ids if item in refs
                    ][:5],
                )
            )
            if len(hits) >= min(limit, 5):
                break
        return EntitySearchResult(hits=hits)

    @limit_tool_payload
    async def read_entity(self, *, program_id, version_id, entity_id, query=None):
        snapshot = await asyncio.to_thread(self.snapshot, program_id, version_id)
        entity = next((item for item in snapshot.entities if item.id == entity_id), None)
        if entity is None:
            raise DomainError(404, "Сущность отсутствует в выбранной версии")
        ids = set(entity.source_ref_ids)
        bounded = _bounded_entity(entity)
        relations = [
            r for r in snapshot.relations if r.source_id == entity_id or r.target_id == entity_id
        ]
        bounded_relations = [_bounded_relation(r) for r in relations[:8]]
        return EntityReadResult(
            entity=bounded,
            matched_context=match_excerpt(json.dumps(entity.attributes, ensure_ascii=False), query)
            if query
            else None,
            truncated=bounded != entity
            or len(relations) > 8
            or len(ids) > 5
            or bounded_relations != relations[:8],
            relations=bounded_relations,
            source_refs=[_bounded_ref(ref) for ref in snapshot.source_refs if ref.id in ids][:5],
        )

    @limit_tool_payload
    async def read_source_fragment(
        self, *, program_id, version_id, source_ref_id, context_paragraphs
    ):
        return await asyncio.to_thread(
            self._read_source_fragment,
            program_id,
            version_id,
            source_ref_id,
            context_paragraphs,
        )

    def _read_source_fragment(self, program_id, version_id, source_ref_id, context_paragraphs):
        snapshot = self.snapshot(program_id, version_id)
        ref = next((item for item in snapshot.source_refs if item.id == source_ref_id), None)
        if ref is None or not ref.artifact_id:
            raise DomainError(404, "Фрагмент источника недоступен")
        with self.pool.connection() as conn:
            row = conn.execute(
                "SELECT storage_key FROM document_artifacts WHERE program_id=%s AND document_id=%s "
                "AND id=%s AND kind='markdown'",
                (program_id, ref.document_id, ref.artifact_id),
            ).fetchone()
        if row is None:
            raise DomainError(404, "Фрагмент источника недоступен")
        path = get_settings().artifact_files_path / str(UUID(row[0]))
        if not path.is_file():
            raise DomainError(503, "Артефакт источника временно недоступен")
        text = path.read_text(encoding="utf-8")
        locator = ref.artifact_locator or ""
        line_number = (
            int(locator.split(":", 1)[1])
            if locator.startswith("line:") and locator[5:].isdigit()
            else None
        )
        if line_number is None or line_number < 1:
            raise DomainError(422, "Формат ссылки на фрагмент не поддерживается")
        lines = text.splitlines()
        start = max(0, line_number - 1 - context_paragraphs * 3)
        end = min(len(lines), line_number + context_paragraphs * 3)
        fragment = lines[line_number - 1][:6000] if line_number <= len(lines) else ""
        with self.pool.connection() as conn:
            document = conn.execute(
                "SELECT original_name FROM source_documents WHERE program_id=%s AND id=%s",
                (program_id, ref.document_id),
            ).fetchone()
        return SourceFragment(
            source_ref=_bounded_ref(ref),
            document_name=document[0] if document else str(ref.document_id),
            text=fragment,
            surrounding_text="\n".join(lines[start:end])[:12000],
        )

    @limit_tool_payload
    async def read_graph_projection(self, *, program_id, version_id, entity_id, depth):
        snapshot = await asyncio.to_thread(self.snapshot, program_id, version_id)
        known = {item.id: item for item in snapshot.entities}
        if entity_id not in known:
            raise DomainError(404, "Сущность отсутствует в выбранной версии")
        selected = [entity_id]
        selected_ids = {entity_id}
        frontier = {entity_id}
        relations = []
        relation_ids = set()
        for _ in range(depth):
            next_frontier = set()
            for relation in snapshot.relations:
                if relation.source_id not in frontier and relation.target_id not in frontier:
                    continue
                if relation.id in relation_ids:
                    continue
                endpoints = {relation.source_id, relation.target_id}
                additions = endpoints - selected_ids
                if len(selected_ids) + len(additions) > 8 or len(relations) >= 12:
                    continue
                if not endpoints.issubset(known):
                    continue
                relations.append(relation)
                relation_ids.add(relation.id)
                selected.extend(
                    item
                    for item in (relation.source_id, relation.target_id)
                    if item not in selected_ids
                )
                selected_ids.update(additions)
                next_frontier.update(additions)
            frontier = next_frontier
            if not frontier:
                break
        refs = {ref for item in selected for ref in known[item].source_ref_ids}
        return GraphProjection(
            entities=[_bounded_entity(known[item]) for item in selected],
            relations=[_bounded_relation(r) for r in relations[:12]],
            source_refs=[_bounded_ref(r) for r in snapshot.source_refs if r.id in refs][:8],
        )

    @limit_tool_payload
    async def get_validation_findings(self, *, program_id, version_id, limit):
        return await asyncio.to_thread(self._get_validation_findings, program_id, version_id, limit)

    def _get_validation_findings(self, program_id, version_id, limit):
        with self.pool.connection() as conn:
            rows = conn.execute(
                "SELECT id,rule_code,type,severity,status,details FROM validation_issues "
                "WHERE program_id=%s AND version_id=%s ORDER BY created_at DESC,id LIMIT %s",
                (program_id, version_id, limit),
            ).fetchall()
        return [
            ValidationFinding.model_validate(
                {
                    "id": row[0],
                    "rule_code": row[1],
                    "type": row[2],
                    "severity": row[3],
                    "status": row[4],
                    "details": _bounded_value(row[5], budget=900),
                }
            )
            for row in rows
        ]


def build_read_port(pool):
    return PostgresChatReadPort(pool)


def configure_read_port(pool):
    configure_worker_read_port(build_read_port(pool))
