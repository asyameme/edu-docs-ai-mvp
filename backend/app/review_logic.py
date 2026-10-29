"""Чистые преобразования: выбор вариантов, хэш содержимого и копирование источников."""

import hashlib
import json
import re
from copy import deepcopy
from uuid import uuid5

from app.domain import SnapshotV2
from app.errors import DomainError
from app.review_contract import Mutation


def digest(value):
    return hashlib.sha256(
        json.dumps(
            value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False
        ).encode()
    ).hexdigest()


def pointer_parts(pointer):
    if not pointer.startswith("/") or re.search(r"~(?![01])", pointer):
        raise ValueError("Invalid JSON Pointer")
    return [part.replace("~1", "/").replace("~0", "~") for part in pointer[1:].split("/")]


def content_hash(snapshot):
    data = (
        snapshot.model_dump(mode="json") if isinstance(snapshot, SnapshotV2) else deepcopy(snapshot)
    )
    data.pop("review", None)
    refs = {ref["id"]: {k: v for k, v in ref.items() if k != "id"} for ref in data["source_refs"]}
    for member in [*data["entities"], *data["relations"]]:
        member["source_ref_ids"] = [refs[key] for key in member["source_ref_ids"]]
    data["source_refs"] = list(refs.values())
    data["source_links"] = [
        {
            **{k: v for k, v in link.items() if k not in {"id", "source_ref_id"}},
            "source": refs[link["source_ref_id"]],
        }
        for link in data["source_links"]
    ]
    return digest(data)


def copy_sources(snapshot, version_id):
    data = (
        snapshot.model_dump(mode="json") if isinstance(snapshot, SnapshotV2) else deepcopy(snapshot)
    )
    refs = {
        item["id"]: str(uuid5(version_id, "source:" + item["id"])) for item in data["source_refs"]
    }
    for ref in data["source_refs"]:
        ref["id"] = refs[ref["id"]]
    for item in [*data["entities"], *data["relations"]]:
        item["source_ref_ids"] = [refs[key] for key in item["source_ref_ids"]]
    for link in data["source_links"]:
        link["id"] = str(uuid5(version_id, "link:" + link["id"]))
        link["source_ref_id"] = refs[link["source_ref_id"]]
    return SnapshotV2.model_validate(data)


def changes_in_base_sources(changes, source_snapshot, base_snapshot):
    """Патчи проверки приводятся к исходным свидетельствам перед пересборкой выбора."""
    source = SnapshotV2.model_validate(source_snapshot)
    base = SnapshotV2.model_validate(base_snapshot)
    base_ids = {
        digest(ref.model_dump(mode="json", exclude={"id"})): str(ref.id) for ref in base.source_refs
    }
    mapping = {
        str(ref.id): base_ids.get(digest(ref.model_dump(mode="json", exclude={"id"})))
        for ref in source.source_refs
    }
    translated = deepcopy(changes)
    for change in translated:
        for kind in ("entity", "relation"):
            item = change.get(kind)
            if item:
                ids = [mapping.get(key) for key in item["source_ref_ids"]]
                if None in ids:
                    raise DomainError(
                        422, "Вариант ссылается на неизвестное исходное свидетельство"
                    )
                item["source_ref_ids"] = ids
    return translated


def apply_changes(snapshot, changes):
    data = (
        snapshot.model_dump(mode="json") if isinstance(snapshot, SnapshotV2) else deepcopy(snapshot)
    )
    changes = [Mutation.model_validate(change) for change in changes]
    touched = {}
    removals = set()
    for change in changes:
        key = (change.target_kind, str(change.target_id), change.field_path or "$")
        payload = change.model_dump(mode="json")
        for previous_key, previous_payload in touched.items():
            if previous_key[:2] == key[:2] and previous_key != key:
                first, second = previous_key[2], key[2]
                if (
                    "$" in (first, second)
                    or first.startswith(second + "/")
                    or second.startswith(first + "/")
                ) and previous_payload != payload:
                    raise DomainError(409, "Выбранные варианты изменяют перекрывающиеся поля")
        if key in touched:
            if touched[key] != payload:
                raise DomainError(409, "Выбранные варианты противоречат друг другу")
            continue
        touched[key] = payload
        collection = data["entities" if change.target_kind == "entity" else "relations"]
        found = next((item for item in collection if item["id"] == str(change.target_id)), None)
        if change.op.startswith("add_"):
            value = change.entity if change.op == "add_entity" else change.relation
            if found:
                raise DomainError(422, "Добавляемый объект уже существует")
            collection.append(value.model_dump(mode="json"))
        elif change.op.startswith("remove_"):
            if found is None:
                raise DomainError(422, "Удаляемый объект отсутствует")
            removals.add((change.target_kind, str(change.target_id)))
        elif change.op == "update_relation":
            if found is None:
                raise DomainError(422, "Изменяемая связь отсутствует")
            found.clear()
            found.update(change.relation.model_dump(mode="json"))
        else:
            if found is None:
                raise DomainError(422, "Изменяемый объект отсутствует")
            parts = pointer_parts(change.field_path)
            target = found
            for part in parts[:-1]:
                if not isinstance(target, dict) or part not in target:
                    raise DomainError(422, "Путь изменения отсутствует")
                target = target[part]
            if not isinstance(target, dict):
                raise DomainError(422, "Изменение должно указывать на атрибут объекта")
            target[parts[-1]] = change.value
    for kind, collection in (("entity", "entities"), ("relation", "relations")):
        data[collection] = [item for item in data[collection] if (kind, item["id"]) not in removals]
    data["source_links"] = [
        item
        for item in data["source_links"]
        if (item["target_kind"], item["target_id"]) not in removals
    ]
    return SnapshotV2.model_validate(data)


def map_analysis_ids(result, job_id, base):
    """Новые бизнес-ID принадлежат подготовке; исторические ID базы сохраняются."""
    data = result.model_dump(mode="json")
    known = {item["id"] for name in ("entities", "relations") for item in base.get(name, [])}
    groups = [
        data["candidate"][name] for name in ("entities", "relations", "source_refs", "source_links")
    ]
    groups += [data["issues"], data["artifacts"]]
    groups += [issue["variants"] for issue in data["issues"]]
    values = [item["id"] for group in groups for item in group]
    if len(values) != len(set(values)):
        raise ValueError("Analysis IDs must be unique across object kinds")
    business_ids = {
        item["id"] for name in ("entities", "relations") for item in data["candidate"][name]
    }
    preserved = known & business_ids
    mapping = {value: str(uuid5(job_id, value)) for value in values if value not in preserved}

    def walk(value, key=""):
        if isinstance(value, dict):
            literal = {
                "attributes",
                "observed_value",
                "metadata",
                "parser_config",
                "ruleset",
                "value",
                "document_id",
                "document_ids",
            }
            return {k: deepcopy(v) if k in literal else walk(v, k) for k, v in value.items()}
        if isinstance(value, list):
            return [walk(item, key) for item in value]
        if isinstance(value, str) and (key == "id" or key.endswith("_id") or key.endswith("_ids")):
            return mapping.get(value, value)
        return value

    return walk(data)
