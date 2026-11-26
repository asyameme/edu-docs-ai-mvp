"""Проверка и воспроизводимое применение выбора к исходному JSON документа."""

from copy import deepcopy

from app.errors import DomainError
from app.parser_document import parse_output
from app.review_contract import (
    CompetencyLinksMutation,
    DocumentMutation,
    SharedIndicatorTextMutation,
)
from app.review_logic import digest, pointer_parts
from worker.validation.validator import competency_links_for_row

_PROTECTED = {"meta", "source_ref", "parse_issues"}


def _equal_json(left, right):
    if isinstance(left, bool) or isinstance(right, bool):
        return type(left) is type(right) and left == right
    if isinstance(left, (int, float)) and isinstance(right, (int, float)):
        return left == right
    if isinstance(left, dict) and isinstance(right, dict):
        return left.keys() == right.keys() and all(
            _equal_json(left[key], right[key]) for key in left
        )
    if isinstance(left, list) and isinstance(right, list):
        return len(left) == len(right) and all(
            _equal_json(a, b) for a, b in zip(left, right, strict=True)
        )
    return type(left) is type(right) and left == right


def _index(token, length):
    if (
        token == "-"
        or not token.isascii()
        or not token.isdecimal()
        or (len(token) > 1 and token.startswith("0"))
    ):
        raise DomainError(422, "JSON Pointer содержит недопустимый индекс массива")
    index = int(token)
    if index >= length:
        raise DomainError(422, "Путь изменения отсутствует")
    return index


def _read(document, parts):
    current = document
    for part in parts:
        if isinstance(current, dict):
            if part not in current:
                raise DomainError(422, "Путь изменения отсутствует")
            current = current[part]
        elif isinstance(current, list):
            current = current[_index(part, len(current))]
        else:
            raise DomainError(422, "Путь изменения отсутствует")
    return current


def _contains_protected(value):
    if isinstance(value, dict):
        return any(key in _PROTECTED or _contains_protected(child) for key, child in value.items())
    if isinstance(value, list):
        return any(_contains_protected(child) for child in value)
    return False


def _write(document, parts, value):
    target = document
    for part in parts[:-1]:
        if isinstance(target, dict):
            if part not in target:
                raise DomainError(422, "Путь изменения отсутствует")
            target = target[part]
        elif isinstance(target, list):
            target = target[_index(part, len(target))]
        else:
            raise DomainError(422, "Путь изменения отсутствует")
    token = parts[-1]
    if isinstance(target, dict):
        if token not in target:
            raise DomainError(422, "Путь изменения отсутствует")
        target[token] = deepcopy(value)
    elif isinstance(target, list):
        target[_index(token, len(target))] = deepcopy(value)
    else:
        raise DomainError(422, "Путь изменения отсутствует")


def _normalize(change):
    mutation = DocumentMutation.model_validate(change)
    parts = pointer_parts(mutation.field_path)
    if not parts:
        raise DomainError(422, "JSON Pointer должен указывать на поле документа")
    if any(part in _PROTECTED for part in parts) or _contains_protected(mutation.value):
        raise DomainError(422, "Нельзя изменять или добавлять защищённые поля")
    return mutation, parts


def validate_document_mutations(documents, changes):
    """Проверяет применимость всех вариантов по отдельности к исходным JSON."""
    for change in changes:
        if change.get("op") == "set_shared_indicator_text":
            _validate_shared_indicator_text(documents, change)
            continue
        if change.get("op") == "set_competency_links":
            _validate_competency_links(documents, change)
            continue
        mutation, parts = _normalize(change)
        identity = str(mutation.document_id)
        if identity not in documents:
            raise DomainError(422, "Вариант ссылается на документ вне исходного пакета")
        current = _read(documents[identity], parts)
        if not _equal_json(current, mutation.expected_old_value):
            raise DomainError(409, "Исходное значение поля изменилось")
        if _contains_protected(current):
            raise DomainError(422, "Нельзя заменять объект, содержащий защищённые данные")


def validate_document_mutation_variants(documents, variants):
    """Проверяет каждую пользовательскую альтернативу как самостоятельный документ."""
    for changes in variants:
        validate_document_mutations(documents, changes)
        if not changes:
            continue
        selected = apply_document_mutations(
            documents, changes, copy_only_affected_documents=True
        )
        affected = {str(change["document_id"]) for change in changes}
        for document_id in affected:
            try:
                parse_output(selected[document_id])
            except (ValueError, TypeError) as error:
                raise DomainError(
                    422, "Вариант не соответствует контракту исходного документа"
                ) from error


def _validate_competency_links(documents, change):
    mutation = CompetencyLinksMutation.model_validate(change)
    identity = str(mutation.document_id)
    document = documents.get(identity)
    if document is None:
        raise DomainError(422, "Вариант ссылается на документ вне исходного пакета")
    records = document.get(mutation.collection, [])
    if mutation.record_index >= len(records):
        raise DomainError(422, "Не найдена исходная строка связей")
    row = records[mutation.record_index]
    links = competency_links_for_row(document, mutation.collection, mutation.record_index)
    if {link["dst_key"] for _, link in links} != set(mutation.expected_codes):
        raise DomainError(409, "Исходный список компетенций изменился")
    if (not row.get("source_ref")
            or any(not code or code != code.strip() for code in mutation.codes)
            or len(mutation.codes) != len(set(mutation.codes))):
        raise DomainError(422, "Вариант содержит некорректные коды или источник")
    return mutation


def _apply_competency_links(result, original, changes, aliases, origins):
    updates = {}
    for change in changes:
        mutation = _validate_competency_links(original, change)
        if set(mutation.codes) == set(mutation.expected_codes):
            continue
        identity = str(mutation.document_id)
        key = (identity, mutation.collection, mutation.record_index)
        codes = {aliases.get(code, code) for code in mutation.codes}
        if key in updates and updates[key] != codes:
            raise DomainError(409, "Для одной строки выбраны разные списки компетенций")
        updates[key] = codes

    known = {
        row["code"] for document in result.values() for row in document.get("competencies", [])
    }
    for identity in {key[0] for key in updates}:
        document = result[identity]
        remove, append = set(), []
        for (_, collection, index), codes in sorted(
            (key, codes) for key, codes in updates.items() if key[0] == identity
        ):
            if not codes <= known:
                raise DomainError(422, "Выбранный список содержит неизвестные компетенции")
            row = document[collection][index]
            existing = competency_links_for_row(document, collection, index)
            remove.update(i for i, relation in existing if relation["dst_key"] not in codes)
            is_plan = collection == "curriculum_items"
            present = {relation["dst_key"] for _, relation in existing}
            for code in sorted(codes - present):
                append.append({
                    "src_entity": "curriculum_item" if is_plan else "discipline",
                    "src_key": row["discipline_name" if is_plan else "name"],
                    "dst_entity": "competency", "dst_key": code, "kind": "формирует",
                    "source_ref": deepcopy(row["source_ref"]),
                })
        kept = [i for i in range(len(document["relations"])) if i not in remove]
        document["relations"] = [document["relations"][i] for i in kept] + append
        if origins is not None:
            origins[identity] = kept + [None] * len(append)


def shared_indicator_groups(parsed):
    """Find distinct indicator codes backed by the same text at one location."""
    rows = parsed.get("indicators", [])
    by_source = {}
    for index, row in enumerate(rows):
        ref, text = row.get("source_ref"), row.get("text")
        if not ref or not ref.get("table") or not ref.get("row") or not ref.get("col") or not text:
            continue
        key = (ref.get("table"), ref.get("row"), ref.get("col"), ref.get("paragraph_index"), text)
        by_source.setdefault(key, []).append(index)
    return {
        tuple(indices) for indices in by_source.values()
        if len(indices) > 1 and len({rows[index]["code"] for index in indices}) == len(indices)
    }


def _validate_shared_indicator_text(documents, change):
    mutation = SharedIndicatorTextMutation.model_validate(change)
    document = documents.get(str(mutation.document_id))
    groups = shared_indicator_groups(document) if document else set()
    if document is None or tuple(mutation.indicator_indices) not in groups:
        raise DomainError(422, "Общий фрагмент индикаторов не совпадает с исходным документом")
    return mutation


def apply_document_mutations(
    original_documents,
    changes,
    *,
    relation_origins=None,
    copy_only_affected_documents=False,
):
    """Каждый полный набор решений заново накладывается на неизменённый импорт."""
    normalized, link_changes, shared = [], [], {}
    for change in changes:
        if change.get("op") == "set_shared_indicator_text":
            decision = _validate_shared_indicator_text(original_documents, change)
            key = (str(decision.document_id), tuple(decision.indicator_indices))
            if key in shared and shared[key] != decision.action:
                raise DomainError(409, "Для общего фрагмента выбраны разные действия")
            shared[key] = decision.action
        elif change.get("op") == "set_competency_links":
            link_changes.append(change)
        else:
            normalized.append(_normalize(change))
    touched = {}
    for mutation, parts in normalized:
        identity = str(mutation.document_id)
        path = (identity, tuple(parts))
        signature = digest(mutation.value)
        for prior, prior_signature in touched.items():
            if prior[0] != identity:
                continue
            left, right = prior[1], path[1]
            if left == right or left[: len(right)] == right or right[: len(left)] == left:
                if prior_signature != signature or left != right:
                    raise DomainError(409, "Выбранные варианты изменяют пересекающиеся поля")
        if path in touched and touched[path] != signature:
            raise DomainError(409, "Выбранные варианты противоречат друг другу")
        touched[path] = signature

    unique = {}
    for mutation, parts in normalized:
        document_id = str(mutation.document_id)
        document = original_documents[document_id]
        current = _read(document, parts)
        if not _equal_json(current, mutation.expected_old_value):
            raise DomainError(409, "Ожидаемое исходное значение больше не совпадает")
        if _contains_protected(current):
            raise DomainError(422, "Нельзя заменять объект, содержащий защищённые данные")
        unique.setdefault((document_id, tuple(parts)), (document, mutation, parts))

    if copy_only_affected_documents:
        affected = {identity for identity, _ in unique}
        affected.update(str(change["document_id"]) for change in link_changes)
        affected.update(identity for identity, _ in shared)
        result = dict(original_documents)
        for identity in affected:
            result[identity] = deepcopy(original_documents[identity])
    else:
        result = deepcopy(original_documents)
    aliases = {}
    kept_fields = {
        (identity, ("indicators", str(index), "text"))
        for (identity, indices), action in shared.items()
        if action == "keep" for index in indices
    }
    kept_fields |= _auto_kept_shared_fields(original_documents, unique, shared)
    for (document_id, _), (_, mutation, parts) in unique.items():
        if (document_id, tuple(parts)) in kept_fields:
            continue
        _write(result[document_id], parts, mutation.value)
        if len(parts) == 3 and parts[0] == "competencies" and parts[-1] == "code":
            aliases[mutation.expected_old_value] = mutation.value
    if link_changes:
        _apply_competency_links(
            result, original_documents, link_changes, aliases, relation_origins
        )
    return result


def _auto_kept_shared_fields(original_documents, unique, shared):
    """Общие тексты индикаторов без явного решения пользователя.

    Один общий текст в документе обслуживает несколько кодов индикаторов, и
    физически в него можно записать только одну формулировку. Спорить об этом
    с пользователем не нужно: если выбранные формулировки кодов совпадают,
    они записываются в общий текст; если различаются — документ сохраняет
    свой исходный общий текст.
    """
    decided = {
        (identity, tuple(indices)) for identity, indices in shared
    }
    kept = set()
    for document_id in {key[0] for key in unique}:
        document = original_documents.get(document_id)
        if document is None:
            continue
        for indices in shared_indicator_groups(document):
            if (document_id, tuple(indices)) in decided:
                continue
            fields = {("indicators", str(index), "text") for index in indices}
            values = {
                mutation.value
                for (identity, parts), (document, mutation, _) in unique.items()
                if identity == document_id and tuple(parts) in fields
            }
            if len(values) > 1:
                kept |= {(document_id, field) for field in fields}
    return kept
