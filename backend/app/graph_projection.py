"""Детерминированное представление неизменяемого SQL-снимка в графе."""

import hashlib
import json
import re

from app.domain import Snapshot, SnapshotV2
from app.review_logic import digest

CONVERTER_REVISION = "snapshot-graph-v1"


def canonical(value):
    return json.dumps(
        value, sort_keys=True, ensure_ascii=False, separators=(",", ":"), allow_nan=False
    )


def relationship_type(value):
    if re.fullmatch(r"[A-Z][A-Z0-9_]{0,63}", value):
        return value
    return "R_" + hashlib.sha256(value.encode()).hexdigest()


def properties(item, program_id, version_id, identity):
    values = {
        "program_id": str(program_id),
        "version_id": str(version_id),
        identity: item["id"],
        "payload": canonical(item),
    }
    # Полный JSON сохраняет null, вложенные структуры и точные десятичные строки.
    for key, value in item.get("attributes", {}).items():
        if value is None or isinstance(value, (dict, list)):
            continue
        if type(value) is int and not -(2**63) <= value < 2**63:
            value = str(value)
        values["attr_" + key] = value
    return values


def project_snapshot(program_id, version_id, raw):
    model = SnapshotV2 if raw.get("schema_version") == 2 else Snapshot
    snapshot = model.model_validate(raw).model_dump(mode="json")
    nodes, edges = [], []
    for item in sorted(snapshot["entities"], key=lambda value: value["id"]):
        props = properties(item, program_id, version_id, "entity_id")
        props["kind"] = item["kind"]
        nodes.append({"label": item["kind"], "props": props})
    for item in sorted(snapshot["relations"], key=lambda value: value["id"]):
        props = properties(item, program_id, version_id, "relation_id")
        props.update(
            source_entity_id=item["source_id"],
            target_entity_id=item["target_id"],
            type=item["type"],
        )
        if "matrix_value" in item.get("attributes", {}):
            value = item["attributes"]["matrix_value"]
            if type(value) is not int or value not in (0, 1):
                raise ValueError("Matrix value must be integer 0 or 1")
            props["matrix_value"] = value
        if item.get("confidence") is not None:
            props["confidence"] = item["confidence"]
        edges.append(
            {
                "type": relationship_type(item["type"]),
                "source_id": item["source_id"],
                "target_id": item["target_id"],
                "props": props,
            }
        )
    return {
        "program_id": str(program_id),
        "version_id": str(version_id),
        "snapshot_hash": digest(snapshot),
        "converter_revision": CONVERTER_REVISION,
        "node_count": len(nodes),
        "relation_count": len(edges),
        "payload_hash": digest({"nodes": nodes, "edges": edges}),
        "nodes": nodes,
        "edges": edges,
    }
