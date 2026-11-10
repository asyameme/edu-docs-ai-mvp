"""Преобразование подтверждённого снимка во вход существующего генератора."""

import json

from backend.contracts.document_v2.graph import EducationalGraph, GraphEdge, GraphNode
from backend.contracts.document_v2.models import SourceRefV2

from app.domain import SnapshotV2
from worker.generation import FieldChange


class GenerationBridgeError(ValueError):
    """Сохранённую версию нельзя без потерь передать генератору."""


def approved_changes(snapshot: SnapshotV2) -> list[FieldChange]:
    if snapshot.review is None:
        return []
    if snapshot.review.confirmation is None:
        raise GenerationBridgeError("Изменения требуют подтверждённой версии программы")
    changes = []
    for decision in snapshot.review.decisions:
        for change in decision.changes:
            if change.get("op") != "set_attribute":
                raise GenerationBridgeError(
                    "Генератор DOCX пока не поддерживает структурные изменения графа"
                )
            changes.append(
                FieldChange(
                    target_kind=change["target_kind"],
                    target_id=str(change["target_id"]),
                    field_path=change["field_path"],
                    new_value=change["value"],
                )
            )
    return changes


def _source_ref(snapshot: SnapshotV2, ref_id):
    ref = next((item for item in snapshot.source_refs if item.id == ref_id), None)
    if ref is None:
        raise GenerationBridgeError("Узел графа ссылается на отсутствующий источник")
    try:
        locator = json.loads(ref.locator)
    except (ValueError, TypeError):
        locator = {}
    if not isinstance(locator, dict):
        locator = {}
    locator = {key: locator[key] for key in SourceRefV2.model_fields if key in locator}
    locator.setdefault("file", str(ref.document_id))
    locator.setdefault("section", ref.section)
    return SourceRefV2.model_validate(locator)


def snapshot_matrix_graph(snapshot: SnapshotV2) -> EducationalGraph:
    """Матрица строится из выбранной версии, а не из прежнего вывода парсера."""
    graph = EducationalGraph()
    program = str(snapshot.metadata.get("program_id") or "program")
    node_ids = {}
    for entity in snapshot.entities:
        attributes = entity.attributes
        if entity.kind == "discipline":
            key = attributes.get("name")
        elif entity.kind == "competency":
            key = attributes.get("code")
        elif entity.kind == "curriculum_item":
            name, semester = attributes.get("discipline_name"), attributes.get("semester")
            key = f"{name}::semester={semester}" if name and semester is not None else None
        else:
            continue
        if not isinstance(key, str) or not key.strip():
            raise GenerationBridgeError(f"У объекта {entity.id} нет ключа для матрицы")
        identity = f"{program}|{entity.kind}|{key}"
        node_ids[entity.id] = identity
        graph.add_node(
            GraphNode(
                id=identity,
                entity_type=entity.kind,
                attributes=attributes,
                source_refs=[_source_ref(snapshot, ref_id) for ref_id in entity.source_ref_ids],
            )
        )
    for relation in snapshot.relations:
        if relation.type != "FORMS_COMPETENCY" or relation.attributes.get("matrix_value") == 0:
            continue
        source = node_ids.get(relation.source_id)
        target = node_ids.get(relation.target_id)
        if source is None or target is None:
            raise GenerationBridgeError("Связь матрицы указывает на неподдерживаемый объект")
        if not relation.source_ref_ids:
            raise GenerationBridgeError("У связи матрицы нет источника")
        graph.add_edge(
            GraphEdge(
                source_id=source,
                target_id=target,
                relation_type="формирует",
                source_ref=_source_ref(snapshot, relation.source_ref_ids[0]),
                document_type="SNAPSHOT",
                semester=relation.attributes.get("semester"),
                value=relation.attributes.get("matrix_value"),
            )
        )
    return graph
