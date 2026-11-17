"""Проверка и воспроизводимое применение выбора к исходному JSON документа."""

from copy import deepcopy

from app.errors import DomainError
from app.parser_document import parse_output
from app.review_contract import DocumentMutation
from app.review_logic import digest, pointer_parts

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
        selected = apply_document_mutations(documents, changes)
        affected = {str(_normalize(change)[0].document_id) for change in changes}
        for document_id in affected:
            try:
                parse_output(selected[document_id])
            except (ValueError, TypeError) as error:
                raise DomainError(
                    422, "Вариант не соответствует контракту исходного документа"
                ) from error


def apply_document_mutations(original_documents, changes):
    """Каждый полный набор решений заново накладывается на неизменённый импорт."""
    normalized = [_normalize(change) for change in changes]
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

    result = deepcopy(original_documents)
    for (document_id, _), (_, mutation, parts) in unique.items():
        _write(result[document_id], parts, mutation.value)
    return result
