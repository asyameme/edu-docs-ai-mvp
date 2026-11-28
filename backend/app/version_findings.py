"""Единое состояние замечаний исходной версии для интерфейса и чата."""

from types import SimpleNamespace
from uuid import UUID, uuid5

from app.source_graph import unresolved_findings


def automatic_findings(issues, metadata, review_id, version_id, analysis_status, check_status):
    """Закрываем наблюдение только после исчезновения неоднозначности в текущем графе."""
    review_id, version_id = str(review_id), str(version_id)
    _, active = unresolved_findings(SimpleNamespace(metadata=metadata), review_id)
    active_ids = {item["id"] for item in active}
    result = []
    for issue in issues:
        if issue["rule_code"] == "SOURCE_RELATION_UNRESOLVED":
            issue = {**issue, "effective_status": (
                "needs_review" if str(issue["id"]) in active_ids else "resolved"
            )}
        result.append(issue)
    existing = {str(item["id"]) for item in result}
    result.extend({**item, "job_id": review_id, "source_version_id": version_id}
                  for item in active if item["id"] not in existing)
    pending = []
    labels = {"processing": "выполняется", "pending": "ожидает запуска", "failed": "не завершён",
              "conflict": "нужно повторить", "not_started": "ожидает запуска"}
    if analysis_status != "completed":
        pending.append("Анализ исходных документов: " + labels.get(analysis_status, "не завершён"))
    if check_status != "completed":
        pending.append("Проверка этой версии: " + labels.get(check_status, "не завершена"))
    if pending:
        result.insert(0, {
            "id": str(uuid5(UUID(str(version_id)), "analysis-status")),
            "job_id": review_id, "source_version_id": version_id,
            "rule_code": "VERSION_ANALYSIS_STATUS", "rule_revision": "version-status-v1",
            "type": "analysis_incomplete", "severity": "warning", "status": "needs_review",
            "requires_decision": False, "variants": [], "source_ref_ids": [],
            "source_document_ids": [],
            "message": "; ".join(pending) + ". Версия сохранена. Список замечаний неполон.",
        })
    return result


def read_automatic_findings(cur, program_id, version_id, review, issues):
    """Читаем только метаданные и состояния задач, без разворачивания всего графа."""
    row = cur.execute(
        "SELECT s.metadata->'unresolved_relations' AS unresolved FROM program_versions v "
        "JOIN graph_states s ON s.id=v.graph_state_id WHERE v.program_id=%s AND v.id=%s",
        (program_id, version_id),
    ).fetchone()
    analysis = cur.execute(
        "SELECT report->>'outcome' AS outcome FROM processing_jobs WHERE program_id=%s AND id=%s",
        (program_id, UUID(review["review_id"])),
    ).fetchone()
    checked = cur.execute(
        "SELECT report->>'outcome' AS outcome FROM processing_jobs WHERE program_id=%s "
        "AND input_version_id=%s AND type='validate' AND parameters->>'purpose'='check' "
        "AND parameters->>'manual_rule_codes' IS NULL ORDER BY created_at DESC,id DESC LIMIT 1",
        (program_id, version_id),
    ).fetchone()
    return automatic_findings(
        issues, {"unresolved_relations": (row or {}).get("unresolved") or []},
        review["review_id"], version_id,
        (analysis or {}).get("outcome") or "processing",
        (checked or {}).get("outcome") or ("processing" if checked else "not_started"),
    )
