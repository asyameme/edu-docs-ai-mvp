"""Исходный граф строится без применения предложенных исправлений."""

from uuid import UUID, uuid5

from app.document_adapter import to_snapshot_v2
from app.document_reconciliation import apply_document_mutations
from app.domain import ReviewState
from app.review_logic import digest


def source_snapshot(program_id, documents, *, review_id, import_job_id, version_id):
    """Сохранить независимые наблюдения документов, включая противоречия."""
    files = [{"document_id": item["document_id"], "output": item["parsed"]} for item in documents]
    snapshot, issues = to_snapshot_v2(
        program_id,
        files,
        original_files=files,
        return_model=True,
    )
    review = ReviewState(
        review_id=review_id,
        import_job_id=import_job_id,
        analysis_version_id=version_id,
        graph_ready=True,
        lifecycle_policy="automatic_source_v2",
    )
    return snapshot.model_copy(update={"review": review}), issues


def unresolved_findings(snapshot, review_id):
    """Представить неполные наблюдения как замечания, сохранив точные источники."""
    revision = "source-relations-v1"
    issues = [
        {
            "id": str(uuid5(UUID(str(review_id)), "unresolved:" + digest(item))),
            "rule_code": "SOURCE_RELATION_UNRESOLVED",
            "rule_revision": revision,
            "type": "unresolved_relation",
            "severity": "warning",
            "status": "needs_review",
            "message": "Связь в исходном документе отсутствует или неоднозначна",
            "source_ref_ids": [item["source_ref_id"]],
            "source_document_ids": [item["document_id"]],
            "requires_decision": False,
            "variants": [],
            "evidence": item,
        }
        for item in snapshot.metadata.get("unresolved_relations", [])
    ]
    coverage = (
        [
            {
                "rule_code": "SOURCE_RELATION_UNRESOLVED",
                "rule_revision": revision,
                "status": "failed",
                "reason": "Сохранены исходные неразрешённые наблюдения связей",
            }
        ]
        if issues
        else []
    )
    return coverage, issues


def apply_document_selections(program_id, documents, snapshot, selections):
    """Перестроить текущие документы, не теряя уже применённые чатовые изменения."""
    from copy import deepcopy

    from app.document_reconciliation import _equal_json, _read, _write
    from app.errors import DomainError
    from app.validation_bridge import project_snapshot_documents

    current = project_snapshot_documents(documents, snapshot)
    current_documents = {item["document_id"]: item["parsed"] for item in current}
    previous = {item.conflict_id: item for item in snapshot.review.decisions}
    adjusted = []
    for selection in selections:
        old = previous.get(selection.conflict_id)
        if old:
            # Отменяем только собственный прежний выбор; чужую правку того же поля не затираем.
            for change in old.changes:
                if change.get("op") != "set_document_value":
                    continue
                parts = [part.replace("~1", "/").replace("~0", "~")
                         for part in change["field_path"][1:].split("/")]
                document = current_documents[str(change["document_id"])]
                actual = _read(document, parts)
                if not any(_equal_json(actual, value) for value in (
                    change["value"], change["expected_old_value"],
                )):
                    raise DomainError(409, "Поле изменилось после выбора; обновите вариант")
                _write(document, parts, change["expected_old_value"])
        changes = deepcopy(selection.changes)
        for change in changes:
            if change.get("op") != "set_competency_links" or not old:
                continue
            prior = next((item for item in old.changes
                          if item.get("op") == "set_competency_links"
                          and all(item.get(key) == change.get(key) for key in (
                              "document_id", "collection", "record_index",
                          ))), None)
            if prior:
                change["expected_codes"] = prior["codes"]
        adjusted.append(selection.model_copy(update={"changes": changes}))
    selections = adjusted
    step_origins = {}
    selected = apply_document_mutations(
        current_documents,
        [change for item in selections for change in item.changes],
        relation_origins=step_origins,
    )
    previous_origins = snapshot.metadata.get("document_relation_origins", {})
    origins = {}
    for item in documents:
        identity = str(item["document_id"])
        previous = previous_origins.get(
            identity,
            list(range(len(item["parsed"].get("relations", [])))),
        )
        step = step_origins.get(
            identity,
            list(range(len(current_documents[identity].get("relations", [])))),
        )
        origins[identity] = [previous[index] if index is not None else None for index in step]
    rebuilt, _ = to_snapshot_v2(
        program_id,
        [{"document_id": identity, "output": parsed} for identity, parsed in selected.items()],
        original_files=documents,
        relation_origins=origins,
        return_model=True,
    )
    replacement_ids = {item.conflict_id for item in selections}
    decisions = [
        item for item in snapshot.review.decisions if item.conflict_id not in replacement_ids
    ] + list(selections)
    review = snapshot.review.model_copy(
        update={
            "decisions": decisions,
            "projected_decision_ids": [item.conflict_id for item in decisions],
            "graph_ready": True,
            "confirmation": None,
        }
    )
    metadata = {
        **rebuilt.metadata,
        "parsed_documents": deepcopy(selected),
        "document_relation_origins": origins,
    }
    old_refs = {item.id: item for item in snapshot.source_refs}
    refs = [
        item.model_copy(
            update={
                "artifact_id": old_refs[item.id].artifact_id,
                "artifact_locator": old_refs[item.id].artifact_locator,
            }
        )
        if item.id in old_refs
        else item
        for item in rebuilt.source_refs
    ]
    return rebuilt.model_copy(update={"review": review, "metadata": metadata, "source_refs": refs})
