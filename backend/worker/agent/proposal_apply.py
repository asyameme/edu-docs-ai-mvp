"""Мост «предложение агента → правка графа и патчи документов».

Слой между ChangeProposal чат-агента и генерацией точечных DOCX-замен
(``worker.generation.patches``). Проверяет каждое предложение против снимка
подтверждённой версии — цель существует, поле на месте, агент видел текущее
значение, указанные источники входят в записанные свидетельства поля, — и
отдаёт два результата: новый снимок версии с применённой правкой и вход
``build_generation_documents`` для замены текста в исходных документах.

Правки применяются по одной: ``apply_change`` возвращает следующий снимок для
новой версии (создание версии и повторную проверку норм выполняет бекенд,
см. ``write_port.ChatWritePort``). Только атрибутные правки ``/attributes/x``;
добавление/удаление сущностей меняют структуру документа и остаются вне области.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal
from uuid import UUID

from pydantic import JsonValue

from app.domain import SnapshotV2
from worker.agent.contracts import ChangeProposal
from worker.generation.patches import FieldChange, FieldEvidence, SourceEvidence

TargetKind = Literal["entity", "relation"]


class ProposalApplicationError(ValueError):
    """Предложение нельзя применить: не совпадает с текущим состоянием версии."""


@dataclass(frozen=True, slots=True)
class ApprovedChange:
    """Одно проверенное предложение агента, готовое к применению."""

    target_kind: TargetKind
    target_id: UUID
    field_path: str
    expected_old_value: JsonValue
    new_value: JsonValue
    reason: str
    evidence_ref_ids: tuple[UUID, ...]


def _unescape(token: str) -> str:
    return token.replace("~1", "/").replace("~0", "~")


def normalize_field_path(field_path: str) -> str:
    """Дополняет путь агента до JSON Pointer ``/attributes/<имя>``.

    Агент видит плоские атрибуты сущности и называет поле как в снимке
    (``hours``); свидетельства хранят полный указатель ``/attributes/hours``.
    Принимаются обе формы; ``$`` (весь объект) и указатели вне ``attributes``
    не отображаются на текст документа и отклоняются.
    """

    if not field_path.strip() or field_path.strip() != field_path:
        raise ProposalApplicationError("Путь поля пуст")
    if field_path == "$":
        raise ProposalApplicationError("Правка всего объекта не поддерживается, укажите поле")
    pointer = field_path if field_path.startswith("/") else f"/attributes/{field_path}"
    tokens = pointer[1:].split("/")
    if any(token == "" for token in tokens):
        raise ProposalApplicationError(f"Некорректный путь поля {field_path!r}")
    if _unescape(tokens[0]) != "attributes":
        raise ProposalApplicationError(
            f"Правки поддерживают только атрибуты сущности, получен путь {field_path!r}"
        )
    if "~" in pointer.replace("~0", "").replace("~1", ""):
        raise ProposalApplicationError(f"Некорректное экранирование в пути поля {field_path!r}")
    return "/attributes/" + "/".join(tokens[1:])


def _attributes_by_id(snapshot: SnapshotV2):
    for entity in snapshot.entities:
        yield ("entity", entity.id), entity.attributes
    for relation in snapshot.relations:
        yield ("relation", relation.id), relation.attributes


def _resolve_target(snapshot: SnapshotV2, kind: TargetKind, target_id: UUID) -> dict:
    for (target_kind, target_id_now), attributes in _attributes_by_id(snapshot):
        if target_kind == kind and target_id_now == target_id:
            return attributes
    raise ProposalApplicationError(
        f"Цель правки ({kind} {target_id}) отсутствует в выбранной версии"
    )


def _pointer_tokens(field_path: str) -> list[str]:
    return [_unescape(token) for token in field_path[1:].split("/")]


def read_attribute(snapshot: SnapshotV2, kind: TargetKind, target_id: UUID, field_path: str):
    """Текущее значение атрибута по указателю; None, если поля нет."""

    attributes = _resolve_target(snapshot, kind, target_id)
    value = attributes
    for token in _pointer_tokens(field_path)[1:]:
        if isinstance(value, list):
            if not token.isascii() or not token.isdigit():
                raise ProposalApplicationError(f"Индекс массива в пути {field_path!r} не число")
            index = int(token)
            if index >= len(value):
                return None
            value = value[index]
        elif isinstance(value, dict):
            if token not in value:
                return None
            value = value[token]
        else:
            return None
    return value


def _json_equal(left: object, right: object) -> bool:
    """Сравнение значений из JSON: bool не равен числу, числа 3 и 3.0 равны."""

    if isinstance(left, bool) != isinstance(right, bool):
        return False
    if isinstance(left, bool) and isinstance(right, bool):
        return left is right
    if isinstance(left, (int, float)) and isinstance(right, (int, float)):
        return float(left) == float(right)
    if isinstance(left, dict) and isinstance(right, dict):
        return left.keys() == right.keys() and all(_json_equal(left[k], right[k]) for k in left)
    if isinstance(left, list) and isinstance(right, list):
        return len(left) == len(right) and all(map(_json_equal, left, right))
    return type(left) is type(right) and left == right


def _evidence_links(snapshot: SnapshotV2, kind: TargetKind, target_id: UUID, field_path: str):
    return [
        link
        for link in snapshot.source_links
        if link.target_kind == kind
        and link.target_id == target_id
        and link.field_path == field_path
    ]


def plan_approved_changes(snapshot: SnapshotV2, proposal: ChangeProposal) -> list[ApprovedChange]:
    """Проверить предложение целиком против снимка и вернуть правки к применению.

    Все проблемы собираются в одно сообщение, чтобы отказ можно было показать
    пользователю целиком. Одинаковые предложения сворачиваются; противоречивые
    правки одного поля отклоняются — как и в ``patches.plan_patches``.
    """

    errors: list[str] = []
    approved: dict[tuple[TargetKind, UUID, str], ApprovedChange] = {}
    for change in proposal.changes:
        label = f"{change.target_kind} {change.target_id}"
        try:
            field_path = normalize_field_path(change.field_path)
        except ProposalApplicationError as error:
            errors.append(f"{label}: {error}")
            continue
        try:
            current = read_attribute(snapshot, change.target_kind, change.target_id, field_path)
        except ProposalApplicationError as error:
            errors.append(str(error))
            continue
        if current is None:
            errors.append(f"{label}: поля {change.field_path!r} нет в текущей версии")
            continue
        if not _json_equal(current, change.expected_old_value):
            errors.append(
                f"{label} {field_path}: агент видел {change.expected_old_value!r}, "
                f"в версии {current!r} — версия изменилась, перечитайте сущность"
            )
            continue
        links = _evidence_links(snapshot, change.target_kind, change.target_id, field_path)
        if not links:
            errors.append(
                f"{label} {field_path}: нет записанных свидетельств источника, "
                "правку нельзя отобразить в документы"
            )
            continue
        evidence_refs = {link.source_ref_id for link in links}
        unverified = [
            str(ref_id) for ref_id in change.source_ref_ids if ref_id not in evidence_refs
        ]
        if unverified:
            errors.append(
                f"{label} {field_path}: источники {', '.join(unverified)} "
                "не относятся к свидетельствам этого поля"
            )
            continue
        key = (change.target_kind, change.target_id, field_path)
        previous = approved.get(key)
        if previous is not None:
            if not _json_equal(previous.new_value, change.new_value):
                errors.append(
                    f"{label} {field_path}: противоречивые предложения "
                    f"({previous.new_value!r} и {change.new_value!r})"
                )
            continue
        approved[key] = ApprovedChange(
            target_kind=change.target_kind,
            target_id=change.target_id,
            field_path=field_path,
            expected_old_value=change.expected_old_value,
            new_value=change.new_value,
            reason=change.reason,
            evidence_ref_ids=tuple(dict.fromkeys(link.source_ref_id for link in links)),
        )
    if errors:
        raise ProposalApplicationError("Предложение отклонено:\n" + "\n".join(errors))
    return list(approved.values())


def apply_change(snapshot: SnapshotV2, change: ApprovedChange) -> SnapshotV2:
    """Новый снимок с одной применённой правкой; исходный снимок не меняется."""

    updated = snapshot.model_copy(deep=True)
    attributes = _resolve_target(updated, change.target_kind, change.target_id)
    tokens = _pointer_tokens(change.field_path)[1:]
    value: object = attributes
    for token in tokens[:-1]:
        value = value[int(token)] if isinstance(value, list) else value[token]
    last = tokens[-1]
    if isinstance(value, list):
        value[int(last)] = change.new_value
    else:
        value[last] = change.new_value
    return updated


def generation_inputs(
    snapshot: SnapshotV2, changes: list[ApprovedChange]
) -> tuple[list[FieldChange], list[FieldEvidence], list[SourceEvidence]]:
    """Вход ``build_generation_documents`` для утверждённых правок.

    Свидетельства отдаются все: факт, записанный в нескольких документах,
    заменяется во всех них сразу (правило слоя патчей). Идентификаторы
    переводятся в строки — контракт генерации строковый.
    """

    refs = {ref.id: ref for ref in snapshot.source_refs}
    field_changes: list[FieldChange] = []
    links: list[FieldEvidence] = []
    used_refs: set[UUID] = set()
    for change in changes:
        field_changes.append(
            FieldChange(
                change.target_kind,
                str(change.target_id),
                change.field_path,
                change.new_value,
            )
        )
        for link in _evidence_links(
            snapshot, change.target_kind, change.target_id, change.field_path
        ):
            used_refs.add(link.source_ref_id)
            links.append(
                FieldEvidence(
                    change.target_kind,
                    str(change.target_id),
                    change.field_path,
                    str(link.source_ref_id),
                    link.observed_value,
                )
            )
    missing = used_refs - set(refs)
    if missing:
        unknown = ", ".join(sorted(map(str, missing)))
        raise ProposalApplicationError(
            f"Свидетельства ссылаются на неизвестные источники: {unknown}"
        )
    sources = [
        SourceEvidence(str(ref.id), str(ref.document_id), ref.locator)
        for ref in snapshot.source_refs
        if ref.id in used_refs
    ]
    return field_changes, links, sources


def touched_document_ids(snapshot: SnapshotV2, changes: list[ApprovedChange]) -> set[UUID]:
    """Документы, которых коснутся правки (для отчёта до применения)."""

    refs = {ref.id: ref.document_id for ref in snapshot.source_refs}
    documents: set[UUID] = set()
    for change in changes:
        change_links = _evidence_links(
            snapshot, change.target_kind, change.target_id, change.field_path
        )
        for link in change_links:
            documents.add(refs[link.source_ref_id])
    return documents
