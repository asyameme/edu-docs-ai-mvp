"""Адаптер чтения закреплённого снимка графовой версии программы."""

import asyncio
import json
from functools import wraps
from uuid import UUID

from pydantic_ai import ModelRetry
from pydantic_core import to_json

from app.config import get_settings
from app.curriculum_workload import HOURS_COMPONENT_PREFIX, with_legacy_curriculum_workload
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
    WorkloadField,
)
from worker.agent.proposal_apply import ApprovedChange, touched_document_ids, unwritable_changes
from worker.agent.read_port import configure_worker_read_port
from worker.generation.docx_patch import DocxPatchError, read_area_text

MAX_TOOL_PAYLOAD_BYTES = 64 * 1024


def limit_tool_payload(function):
    """Проверяет весь сериализованный ответ до выхода из Temporal Activity."""

    @wraps(function)
    async def checked(*args, **kwargs):
        try:
            result = await function(*args, **kwargs)
        except DomainError as error:
            if error.status in (404, 422):
                raise ModelRetry(
                    f"Не удалось выполнить чтение: {error.detail}. "
                    "Проверь идентификатор и координаты в выбранной версии, "
                    "затем повтори запрос или сообщи пользователю, что данные не найдены."
                ) from error
            raise
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


# Доля одного вложенного показателя (часы по видам нагрузки, списки семестров):
# при меньшей доле «часы по видам» обрезались до трёх записей из шести, и модель
# честно отвечала, что остальные виды «не детализированы».
_CHILD_SHARE = 3
_CHILD_MIN_BYTES = 300


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
            child_budget = min(remaining, max(_CHILD_MIN_BYTES, budget // _CHILD_SHARE))
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
            safe_value = _bounded_value(
                item, min(remaining, max(_CHILD_MIN_BYTES, budget // _CHILD_SHARE)), depth + 1
            )
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
        if entity.kind == "curriculum_item" and "hours_by_kind" not in entity.attributes:
            snapshot = await asyncio.to_thread(
                with_legacy_curriculum_workload, snapshot, program_id, {entity_id},
                Repository(self.pool).document_content,
            )
            entity = next(item for item in snapshot.entities if item.id == entity_id)
        ids = set(entity.source_ref_ids)
        bounded = _bounded_entity(entity)
        relations = [
            r for r in snapshot.relations if r.source_id == entity_id or r.target_id == entity_id
        ]
        bounded_relations = [_bounded_relation(r) for r in relations[:8]]
        workload = []
        if entity.kind in {"curriculum_item", "discipline"} and "hours_by_kind" in entity.attributes:
            for link in snapshot.source_links:
                if (
                    link.target_id == entity_id
                    and link.field_path.startswith(HOURS_COMPONENT_PREFIX)
                    and link.observed_value.get("cell_text") is not None
                ):
                    name = link.field_path[len(HOURS_COMPONENT_PREFIX):]
                    name = name.replace("~1", "/").replace("~0", "~")
                    workload.append(WorkloadField(
                        label=name, hours=entity.attributes["hours_by_kind"][name],
                        field_path=link.field_path, source_ref_id=link.source_ref_id,
                    ))
        hours_total_write_error = None
        if workload:
            hours_total_write_error = "Итог рассчитывается автоматически из граф нагрузки."
        elif entity.kind == "curriculum_item" and type(entity.attributes.get("hours_total")) is int:
            hours_total_write_error = await asyncio.to_thread(
                self._curriculum_hours_write_error, program_id, snapshot, entity,
            )
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
            hours_total_write_error=hours_total_write_error,
            editable_workload=workload,
        )

    def _curriculum_hours_write_error(self, program_id, snapshot, entity):
        hours = entity.attributes["hours_total"]
        # Проверяем замену на временной копии DOCX. Никаких версий и файлов не записываем.
        change = ApprovedChange(
            "entity", entity.id, "/attributes/hours_total", hours, hours + 1, "", ()
        )
        document_ids = sorted(touched_document_ids(snapshot, [change]))
        if not document_ids:
            return "У общего объёма часов нет источника для прямой замены в учебном плане."
        with self.pool.connection() as conn:
            rows = conn.execute(
                "SELECT id,content FROM source_documents WHERE program_id=%s AND id=ANY(%s)",
                (program_id, document_ids),
            ).fetchall()
        documents = {str(row[0]): bytes(row[1]) for row in rows}
        problems = unwritable_changes(snapshot, [change], documents)
        return "; ".join(problems) if problems else None

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
        if ref is None:
            raise DomainError(404, "Фрагмент источника недоступен")
        if ref.artifact_id and ref.artifact_locator and ref.artifact_locator.startswith("line:"):
            with self.pool.connection() as conn:
                row = conn.execute(
                    "SELECT storage_key FROM document_artifacts WHERE program_id=%s "
                    "AND document_id=%s AND id=%s AND kind='markdown'",
                    (program_id, ref.document_id, ref.artifact_id),
                ).fetchone()
            if row is None:
                raise DomainError(404, "Фрагмент источника недоступен")
            path = get_settings().artifact_files_path / str(UUID(row[0]))
            if not path.is_file():
                raise DomainError(503, "Артефакт источника временно недоступен")
            text = path.read_text(encoding="utf-8")
            locator = ref.artifact_locator
            line_number = (
                int(locator.split(":", 1)[1])
                if locator.startswith("line:") and locator[5:].isdigit()
                else None
            )
            if line_number is None or line_number < 1:
                raise DomainError(422, "Формат ссылки на фрагмент не поддерживается")
            lines = text.splitlines()
            if line_number > len(lines):
                raise DomainError(404, "Фрагмент источника не найден")
            start = max(0, line_number - 1 - context_paragraphs * 3)
            end = min(len(lines), line_number + context_paragraphs * 3)
            fragment = lines[line_number - 1][:6000]
            surrounding = "\n".join(lines[start:end])[:12000] if context_paragraphs else ""
            with self.pool.connection() as conn:
                document = conn.execute(
                    "SELECT original_name FROM source_documents WHERE program_id=%s AND id=%s",
                    (program_id, ref.document_id),
                ).fetchone()
            document_name = document[0] if document else str(ref.document_id)
        else:
            try:
                coordinates = json.loads(ref.locator)
            except (TypeError, json.JSONDecodeError) as error:
                raise DomainError(422, "Координаты фрагмента источника повреждены") from error
            if not isinstance(coordinates, dict) or not any(
                coordinates.get(key) is not None
                for key in ("paragraph_index", "table", "sheet_name", "cell")
            ):
                raise DomainError(422, "Для фрагмента источника нет точных координат")
            from app.repository import Repository

            document = Repository(self.pool).document_content(program_id, ref.document_id)
            if not str(document["original_name"]).lower().endswith(".docx"):
                raise DomainError(422, "Чтение фрагмента этого формата пока не поддерживается")
            try:
                paragraphs = read_area_text(document["content"], coordinates)
            except DocxPatchError as error:
                raise DomainError(
                    422, "Координаты фрагмента источника не соответствуют DOCX"
                ) from error
            text = "\n".join(part for part in paragraphs if part)
            if not text:
                raise DomainError(404, "В указанном фрагменте источника нет текста")
            fragment = text[:6000]
            surrounding = ""
            document_name = document["original_name"]
        return SourceFragment(
            source_ref=_bounded_ref(ref),
            document_name=document_name,
            text=fragment,
            surrounding_text=surrounding,
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
            version = conn.execute(
                "SELECT review FROM program_versions WHERE program_id=%s AND id=%s",
                (program_id, version_id),
            ).fetchone()
            if version is None:
                raise DomainError(404, "Версия программы не найдена")

            review = version[0] or {}
            review_id = review.get("review_id")
            confirmation = review.get("confirmation") or {}
            check_job_id = confirmation.get("check_job_id")
            if not check_job_id:
                latest_check = conn.execute(
                    """SELECT id FROM processing_jobs WHERE program_id=%s
                       AND input_version_id=%s AND type='validate'
                       AND parameters->>'purpose'='check'
                       AND parameters->>'manual_rule_codes' IS NULL
                       AND report->>'outcome'='completed'
                       ORDER BY created_at DESC,id DESC LIMIT 1""",
                    (program_id, version_id),
                ).fetchone()
                check_job_id = latest_check[0] if latest_check else None

            job_ids = [UUID(str(value)) for value in (review_id, check_job_id) if value]
            if job_ids:
                rows = conn.execute(
                    """SELECT id,job_id,rule_code,type,severity,status,details
                       FROM validation_issues WHERE program_id=%s
                       AND (version_id=%s OR job_id=ANY(%s))
                       ORDER BY created_at DESC,id LIMIT %s""",
                    (program_id, version_id, job_ids, limit),
                ).fetchall()
            else:
                rows = conn.execute(
                    """SELECT id,job_id,rule_code,type,severity,status,details
                       FROM validation_issues WHERE program_id=%s AND version_id=%s
                       ORDER BY created_at DESC,id LIMIT %s""",
                    (program_id, version_id, limit),
                ).fetchall()

        decisions = {
            str(item.get("conflict_id"))
            for item in review.get("decisions", [])
            if isinstance(item, dict) and item.get("conflict_id")
        }
        accepted = {
            str(value)
            for value in confirmation.get("accepted_warning_ids", [])
        }
        has_current_check = check_job_id is not None
        findings = []
        for row in rows:
            issue_id, job_id, rule_code, issue_type, severity, status, details = row
            details = details or {}
            if (
                has_current_check
                and review_id
                and str(job_id) == str(review_id)
                and str(rule_code).startswith("R-08")
                and (
                    not details.get("requires_decision")
                    or str(issue_id) in decisions
                )
            ):
                continue
            effective_status = (
                "resolved"
                if str(issue_id) in decisions
                else "accepted"
                if str(issue_id) in accepted
                else status
            )
            findings.append(
                ValidationFinding.model_validate(
                    {
                        "id": issue_id,
                        "rule_code": rule_code,
                        "type": issue_type,
                        "severity": severity,
                        "status": effective_status,
                        "details": _bounded_value(details, budget=900),
                    }
                )
            )
        return findings


def build_read_port(pool):
    return PostgresChatReadPort(pool)


def configure_read_port(pool):
    configure_worker_read_port(build_read_port(pool))
