"""Проверка пригодности сохранённой версии без принятия предупреждений."""

from app.domain import SnapshotV2
from app.errors import DomainError
from app.review_contract import CheckResult
from app.review_logic import content_hash, digest
from app.review_projection import project_legacy_review_result


def is_automatic_version(snapshot: SnapshotV2) -> bool:
    return snapshot.review is not None


def pending_document_choices(review):
    """Документные решения, ещё не перенесённые в граф контрольной точки."""
    from app.review_contract import DOCUMENT_MUTATION_OPS

    projected = set(review.projected_decision_ids)
    return [
        item
        for item in review.decisions
        if item.conflict_id not in projected
        and any(change.get("op") in DOCUMENT_MUTATION_OPS for change in item.changes)
    ]


def require_generation_check(snapshot, program_id, version_id, checked):
    """Проверить точный вход, полноту и целостность, сохраняя замечания открытыми."""
    if not is_automatic_version(snapshot) or not snapshot.review.graph_ready:
        raise DomainError(409, "Нет сохранённого графа автоматической версии")
    if pending_document_choices(snapshot.review):
        raise DomainError(
            409, "Нажмите «Далее» на экране выбора, чтобы применить сохранённые решения"
        )
    if checked is None:
        raise DomainError(409, "Для выгрузки нужна завершённая проверка этой версии")
    parameters = checked["parameters"]
    if (
        str(checked["program_id"]) != str(program_id)
        or str(checked["input_version_id"]) != str(version_id)
        or parameters.get("purpose") != "check"
        or parameters.get("review_id") != str(snapshot.review.review_id)
        or parameters.get("manual_rule_codes") is not None
        or checked["report"].get("outcome") != "completed"
        or parameters.get("content_hash") != content_hash(snapshot)
    ):
        raise DomainError(409, "Нужна завершённая автоматическая проверка точной версии")
    result = CheckResult.model_validate(project_legacy_review_result(checked["report"]["result"]))
    if digest(result.ruleset.model_dump(mode="json")) != digest(parameters["ruleset"]):
        raise DomainError(409, "Набор правил проверенной версии изменился")
    coverage = {item.rule_code: item.status for item in result.coverage}
    if any(
        coverage.get(code) not in {"passed", "failed"} for code in result.ruleset.required_rules
    ):
        raise DomainError(409, "Есть обязательные проверки без окончательного результата")
    if any(item.severity == "blocking" for item in result.issues):
        raise DomainError(409, "Проверка обнаружила нарушение целостности графа")
    refs = {item.id for item in snapshot.source_refs}
    if any(
        not item.source_ref_ids or not set(item.source_ref_ids).issubset(refs)
        for item in [*snapshot.entities, *snapshot.relations]
    ) or any(ref.document_id not in snapshot.document_ids for ref in snapshot.source_refs):
        raise DomainError(409, "У объектов графа отсутствуют достоверные источники")
    return result


def version_capabilities(snapshot, program_id, version_id, checked, analysis_status):
    """Разделить сохранение версии, завершённость проверок и готовность пакета."""
    if not is_automatic_version(snapshot):
        return None
    reason = None
    try:
        require_generation_check(snapshot, program_id, version_id, checked)
    except DomainError as error:
        reason = error.detail
    return {
        "policy": snapshot.review.lifecycle_policy,
        "saved": snapshot.review.graph_ready,
        "needs_projection": bool(pending_document_choices(snapshot.review)),
        "is_source": str(version_id) == str(snapshot.review.analysis_version_id),
        "analysis_status": analysis_status,
        "check_job_id": checked["id"] if checked else None,
        "check_status": (
            checked["report"].get("outcome") or "processing" if checked else "not_started"
        ),
        "can_generate": reason is None,
        "generation_reason": reason,
        "unresolved_relations": len(snapshot.metadata.get("unresolved_relations", [])),
    }


def compact_version_capabilities(
    review, metadata, graph_status, graph_hash, program_id, version_id, checked,
    analysis_status,
):
    """Оценить состояние версии по уже проверенному состоянию графа без его загрузки."""
    reason = None
    if not review.graph_ready or graph_status != "sealed":
        reason = "Нет сохранённого графа автоматической версии"
    elif pending_document_choices(review):
        reason = "Нажмите «Далее» на экране выбора, чтобы применить сохранённые решения"
    elif checked is None:
        reason = "Для выгрузки нужна завершённая проверка этой версии"
    else:
        parameters = checked["parameters"]
        report = checked["report"]
        if (
            str(checked["program_id"]) != str(program_id)
            or str(checked["input_version_id"]) != str(version_id)
            or parameters.get("purpose") != "check"
            or parameters.get("review_id") != str(review.review_id)
            or parameters.get("manual_rule_codes") is not None
            or report.get("outcome") != "completed"
            or parameters.get("content_hash") != graph_hash
        ):
            reason = "Нужна завершённая автоматическая проверка точной версии"
        else:
            result = CheckResult.model_validate(
                project_legacy_review_result(report["result"])
            )
            if digest(result.ruleset.model_dump(mode="json")) != digest(parameters["ruleset"]):
                reason = "Набор правил проверенной версии изменился"
            else:
                coverage = {item.rule_code: item.status for item in result.coverage}
                if any(
                    coverage.get(code) not in {"passed", "failed"}
                    for code in result.ruleset.required_rules
                ):
                    reason = "Есть обязательные проверки без окончательного результата"
                elif any(item.severity == "blocking" for item in result.issues):
                    reason = "Проверка обнаружила нарушение целостности графа"
    return {
        "policy": review.lifecycle_policy,
        "saved": review.graph_ready and graph_status == "sealed",
        "needs_projection": bool(pending_document_choices(review)),
        "is_source": str(version_id) == str(review.analysis_version_id),
        "analysis_status": analysis_status,
        "check_job_id": checked["id"] if checked else None,
        "check_status": (
            checked["report"].get("outcome") or "processing" if checked else "not_started"
        ),
        "can_generate": reason is None,
        "generation_reason": reason,
        "unresolved_relations": len(metadata.get("unresolved_relations", [])),
    }
