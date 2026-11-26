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
        snapshot.model_dump(mode="json") if isinstance(snapshot, SnapshotV2) else dict(snapshot)
    )
    data.pop("review", None)
    refs = {ref["id"]: {k: v for k, v in ref.items() if k != "id"} for ref in data["source_refs"]}
    for field in ("entities", "relations"):
        data[field] = [
            {**member, "source_ref_ids": [refs[key] for key in member["source_ref_ids"]]}
            for member in data[field]
        ]
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


def custom_decision_changes(details, custom_value, *, rule_code):
    """Собирает безопасные изменения для собственного ответа на вопрос сверки.

    Для текстовых правил шаблоном служит первый готовый вариант. R-01 отдельно
    выравнивает списки компетенций по введённому пользователем коду.
    """

    if rule_code.startswith("R-01"):
        return _custom_competency_link_changes(details, custom_value)
    if not rule_code.startswith(("R-07", "R-08")):
        raise DomainError(
            422, "Свой вариант доступен только для правил формулировок (R-07, R-08)"
        )
    variants = details.get("variants") or []
    template = variants[0]["changes"] if variants else []
    if not template:
        raise DomainError(422, "Для своего варианта нет шаблона полей документов")
    if any(change.get("op") != "set_document_value" for change in template):
        raise DomainError(
            422, "Свой вариант допустим только для замены текста в документах"
        )
    return [{**change, "value": custom_value} for change in template]


def _custom_competency_link_changes(details, custom_value):
    """Replace one differing competency code in both matched source lists."""
    code = custom_value.strip()
    if not code or len(code) > 200 or re.search(r"[\s,;]", code):
        raise DomainError(422, "Введите один код компетенции без пробелов и разделителей")

    source_variants = [
        variant for variant in details.get("variants", [])
        if str(variant.get("label", "")).startswith("Список из ")
    ]
    targets = {}
    for variant in source_variants:
        changes = variant.get("changes") or []
        if len(changes) != 1 or changes[0].get("op") != "set_competency_links":
            raise DomainError(422, "Не удалось однозначно определить списки для собственного кода")
        change = changes[0]
        key = (
            str(change.get("document_id")),
            change.get("collection"),
            change.get("record_index"),
        )
        targets.setdefault(key, change)

    plan_targets = [
        change for change in targets.values()
        if change.get("collection") == "curriculum_items"
    ]
    document_targets = [
        change for change in targets.values()
        if change.get("collection") == "disciplines"
    ]
    if len(plan_targets) != 1 or len(document_targets) != 1:
        raise DomainError(
            422,
            "Свой код можно сохранить, когда найдено по одной соответствующей строке "
            "учебного плана и РПД",
        )

    plan, document = plan_targets[0], document_targets[0]
    plan_codes = set(plan.get("expected_codes") or [])
    document_codes = set(document.get("expected_codes") or [])
    common = plan_codes & document_codes
    plan_only = plan_codes - document_codes
    document_only = document_codes - plan_codes
    if len(plan_only) > 1 or len(document_only) > 1 or not (plan_only or document_only):
        raise DomainError(
            422,
            "Свой код можно сохранить автоматически, если в каждом списке отличается "
            "не больше одного кода",
        )
    if code in common:
        raise DomainError(
            422,
            "Указанный код уже есть в обоих списках; введите код вместо отличающегося",
        )

    def align(change, own_differences):
        aligned = [code if item in own_differences else item for item in change["expected_codes"]]
        if not own_differences:
            aligned.append(code)
        return {**change, "codes": aligned}

    return [align(plan, plan_only), align(document, document_only)]


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
        signature = digest(change.model_dump(mode="json"))
        for previous_key, previous_signature in touched.items():
            if previous_key[:2] == key[:2] and previous_key != key:
                first, second = previous_key[2], key[2]
                if (
                    "$" in (first, second)
                    or first.startswith(second + "/")
                    or second.startswith(first + "/")
                ) and previous_signature != signature:
                    raise DomainError(409, "Выбранные варианты изменяют перекрывающиеся поля")
        if key in touched:
            if touched[key] != signature:
                raise DomainError(409, "Выбранные варианты противоречат друг другу")
            continue
        touched[key] = signature
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


def map_analysis_v2_ids(result, job_id, documents):
    """Закрепляет IDs результата без графа за неизменяемой попыткой анализа."""
    data = result.model_dump(mode="json")
    allowed_documents = {str(item["document_id"]) for item in documents}
    if any(str(item.document_id) not in allowed_documents for item in result.artifacts):
        raise ValueError("Analysis artifact references a document outside the pinned import")
    for issue in result.issues:
        if not set(map(str, issue.source_document_ids)).issubset(allowed_documents):
            raise ValueError("Analysis issue references a document outside the pinned import")
        for variant in issue.variants:
            for change in variant.changes:
                if str(change.document_id) not in allowed_documents:
                    raise ValueError(
                        "Analysis variant references a document outside the pinned import"
                    )
    groups = [data["issues"], data["artifacts"]]
    groups += [issue["variants"] for issue in data["issues"]]
    values = [item["id"] for group in groups for item in group]
    if len(values) != len(set(values)):
        raise ValueError("Analysis IDs must be unique across object kinds")
    mapping = {value: str(uuid5(job_id, value)) for value in values}

    def walk(value, key=""):
        if isinstance(value, dict):
            literal = {
                "config",
                "ruleset",
                "value",
                "expected_old_value",
                "document_id",
                "source_document_ids",
            }
            return {k: deepcopy(v) if k in literal else walk(v, k) for k, v in value.items()}
        if isinstance(value, list):
            return [walk(item, key) for item in value]
        if isinstance(value, str) and (key == "id" or key.endswith("_id") or key.endswith("_ids")):
            return mapping.get(value, value)
        return value

    return walk(data)
