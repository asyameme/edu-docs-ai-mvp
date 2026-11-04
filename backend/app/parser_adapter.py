"""Преобразование наблюдений ML без выбора правильного значения при расхождениях."""

import json
from collections import defaultdict
from uuid import UUID, uuid5

from app.domain import Snapshot

GROUPS = {
    "disciplines": ("discipline", "name"),
    "competencies": ("competency", "code"),
    "indicators": ("indicator", "code"),
    "learning_outcomes": ("learning_outcome", "local_id"),
    "assessment_tools": ("assessment_tool", "name"),
    "curriculum_items": ("curriculum_item", "discipline_name"),
    "course_content": ("course_topic", "topic"),
    "score_scale": ("score_scale", "grade_spbu"),
}


def to_snapshot(program_id, files):
    namespace = UUID(str(program_id))
    entities, relations, refs, issues, unresolved = [], [], {}, [], []
    local, all_keys = defaultdict(list), defaultdict(list)

    def identity(value):
        return str(uuid5(namespace, value))

    def evidence(document_id, source):
        locator = json.dumps(source, sort_keys=True, ensure_ascii=False)
        ref_id = identity(f"source:{document_id}:{locator}")
        refs[ref_id] = {
            "id": ref_id,
            "document_id": str(document_id),
            "section": source.get("section"),
            "locator": locator,
        }
        return ref_id

    for item in files:
        output = item["output"]
        if not output:
            continue
        document_id = str(item["document_id"])
        groups = {**{name: output.get(name, []) for name in GROUPS}}
        if output.get("program"):
            groups["programs"] = [output["program"]]
        for group, members in groups.items():
            kind, key_field = GROUPS.get(group, ("program", "name"))
            for index, member in enumerate(members):
                key = str(member.get(key_field) or kind)
                entity_id = identity(f"entity:{document_id}:{kind}:{key}:{index}")
                entities.append(
                    {
                        "id": entity_id,
                        "kind": kind,
                        "attributes": {k: v for k, v in member.items() if k != "source_ref"},
                        "source_ref_ids": [evidence(document_id, member["source_ref"])],
                    }
                )
                local[document_id, kind, key].append(entity_id)
                all_keys[kind, key].append(entity_id)
        for issue in output.get("parse_issues", []):
            issues.append({**issue, "document_id": document_id})
    for item in files:
        if not item["output"]:
            continue
        document_id = str(item["document_id"])
        for index, relation in enumerate(item["output"].get("relations", [])):
            endpoints = []
            for side in ("src", "dst"):
                kind, key = relation[f"{side}_entity"], relation[f"{side}_key"]
                candidates = local[document_id, kind, key] or all_keys[kind, key]
                endpoints.append(candidates[0] if len(candidates) == 1 else None)
            if None in endpoints:
                unresolved.append({"document_id": document_id, **relation})
                issues.append(
                    {
                        "category": "unresolved_relation",
                        "severity": "warning",
                        "document_id": document_id,
                        "message": "Связь не установлена: сущности отсутствуют или неоднозначны",
                    }
                )
                continue
            relations.append(
                {
                    "id": identity(f"relation:{document_id}:{index}"),
                    "source_id": endpoints[0],
                    "target_id": endpoints[1],
                    "type": relation["kind"],
                    "source_ref_ids": [evidence(document_id, relation["source_ref"])],
                }
            )
    snapshot = Snapshot.model_validate(
        {
            "entities": entities,
            "relations": relations,
            "source_refs": list(refs.values()),
            "document_ids": [str(item["document_id"]) for item in files],
            "metadata": {"unresolved_relations": unresolved, "parse_issues": issues},
        }
    )
    return snapshot.model_dump(mode="json"), issues
