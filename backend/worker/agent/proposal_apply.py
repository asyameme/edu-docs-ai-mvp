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

from collections.abc import Mapping
from copy import deepcopy
from dataclasses import dataclass
from typing import Literal
from uuid import UUID

from pydantic import JsonValue

from app.curriculum_workload import (
    HOURS_COMPONENT_PREFIX,
    HOURS_TOTAL_PATH,
    WORKLOAD_ENTITY_KINDS,
    with_editable_workload,
)
from app.domain import SnapshotV2
from worker.agent.contracts import Answer, ChangeProposal, Clarification, ProposedAttributeChange
from worker.generation.patches import FieldChange, FieldEvidence, SourceEvidence

TargetKind = Literal["entity", "relation"]

# Импорт пока не записывает единицу измерения в observed_value (app/document_adapter).
# Часы в тексте РПД обычно идут с «ч», поэтому для них нужен фолбэк. ЗЕТ в
# таблицах учебной работы находятся в отдельной колонке: в ячейке стоит число,
# а единица задана заголовком. Добавлять «ЗЕТ» к числу при поиске нельзя.
# Юнит из свидетельства всегда приоритетен; фолбэк — только для часов.
_UNIT_BY_FIELD_PREFIX = (("hours", "ч"),)


def _fallback_unit(field_path: str) -> str | None:
    tail = _unescape(field_path.rsplit("/", 1)[-1])
    for prefix, unit in _UNIT_BY_FIELD_PREFIX:
        if tail.startswith(prefix):
            return unit
    return None


class ProposalApplicationError(ValueError):
    """Предложение нельзя применить: не совпадает с текущим состоянием версии."""


def prepare_rpd_workload_alignment(snapshot, program_id, request, read_document):
    """Подготовить план из наблюдений DOCX для запроса об изменении итога РПД."""
    title = " ".join(request.discipline_name.split())
    targets = [
        item for item in snapshot.entities
        if item.kind == "discipline"
        and " ".join(item.attributes.get("name", "").split()).casefold() == title.casefold()
        and (not request.semesters
             or set(request.semesters) & set(item.attributes.get("semesters") or []))
    ]
    target_ids = {item.id for item in targets}
    refs = {ref.id: ref.document_id for ref in snapshot.source_refs}
    documents = {
        refs[link.source_ref_id] for link in snapshot.source_links
        if link.target_id in target_ids and link.field_path == HOURS_TOTAL_PATH
    }
    covered_semesters = {
        semester for item in targets for semester in (item.attributes.get("semesters") or [])
    }
    if len(documents) != 1 or set(request.semesters or ()) - covered_semesters:
        return Clarification(
            kind="clarification",
            message=f"Не удалось однозначно найти РПД «{title}» для этой правки.",
            questions=[
                "Как называется дисциплина в РПД и для каких семестров нужно изменить часы?"
            ],
        )
    cached_documents = {}

    def read_source(owner, document_id):
        if document_id not in cached_documents:
            cached_documents[document_id] = read_document(owner, document_id)
        return cached_documents[document_id]

    hydrated = with_editable_workload(snapshot, program_id, target_ids, read_source)
    changes = []
    for item in targets:
        current_total = item.attributes.get("hours_total")
        semesters = item.attributes.get("semesters") or []
        if request.semesters and not set(semesters).issubset(request.semesters):
            return Answer(
                kind="answer",
                message=f"В РПД «{title}» нагрузка хранится общей для семестров "
                f"{', '.join(map(str, semesters))}. Для правки нужен общий объём "
                "для этих семестров.",
            )
        if current_total == request.hours:
            continue
        fields = [
            link for link in hydrated.source_links
            if link.target_id == item.id and link.field_path.startswith(HOURS_COMPONENT_PREFIX)
            and not link.observed_value.get("workload_mirror")
        ]
        if fields:
            current = {
                link.field_path: read_attribute(hydrated, "entity", item.id, link.field_path)
                for link in fields
            }
            expected = {
                link.field_path: (
                    current[link.field_path]
                    if current[link.field_path] != link.observed_value["value"]
                    else link.observed_value.get("semester_hours")
                )
                for link in fields
            }
            if None in expected.values() or sum(expected.values()) != request.hours:
                workload = ", ".join(
                    f"{_unescape(path.removeprefix(HOURS_COMPONENT_PREFIX))} — {value} ч"
                    for path, value in current.items()
                )
                return Clarification(
                    kind="clarification",
                    message=f"Семестр {semesters[0]}, текущая нагрузка в РПД «{title}». "
                    f"{workload}.",
                    questions=[f"Какие графы изменить, чтобы получить {request.hours} ч?"],
                )
            for link in fields:
                path = link.field_path
                if current[path] == expected[path]:
                    continue
                label = _unescape(path.removeprefix(HOURS_COMPONENT_PREFIX))
                changes.append(ProposedAttributeChange(
                    target_kind="entity", target_id=item.id, field_path=path,
                    expected_old_value=current[path], new_value=expected[path],
                    reason=f"Семестр {semesters[0]}. "
                    f"В строке семестра {label} — {expected[path]} ч. "
                    f"Перенос этого значения в «Итого» даёт {request.hours} ч.",
                    source_ref_ids=[link.source_ref_id],
                ))
        else:
            links = _evidence_links(hydrated, "entity", item.id, HOURS_TOTAL_PATH)
            if not links or current_total is None:
                return Answer(
                    kind="answer",
                    message=f"Для общего объёма РПД «{title}» "
                    "нет ячейки с подтверждённым значением.",
                )
            changes.append(ProposedAttributeChange(
                target_kind="entity", target_id=item.id, field_path=HOURS_TOTAL_PATH,
                expected_old_value=current_total, new_value=request.hours,
                reason=f"Общий объём РПД «{title}» по запросу пользователя.",
                source_ref_ids=[link.source_ref_id for link in links],
            ))
    if not changes:
        return Answer(kind="answer", message=f"В РПД «{title}» уже указано {request.hours} ч.")
    proposal = ChangeProposal(
        kind="change_proposal",
        summary=f"Привести нагрузку РПД «{title}» к {request.hours} ч.",
        changes=changes,
    )
    try:
        approved = plan_approved_changes(hydrated, proposal)
    except ProposalApplicationError as error:
        return Answer(kind="answer", message=str(error))
    sources = {
        str(doc_id): bytes(read_source(program_id, doc_id)["content"])
        for doc_id in touched_document_ids(hydrated, approved)
    }
    problems = unwritable_changes(hydrated, approved, sources)
    if problems:
        return Answer(
            kind="answer",
            message="Правку нельзя записать в исходный РПД. " + ". ".join(dict.fromkeys(problems)),
        )
    return proposal


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


def _compact_workload_pointer(field_path: str) -> str | None:
    """Expand the dotted workload shorthand into its nested JSON Pointer."""
    for prefix in (
        "/attributes/hours_by_kind.",
        "attributes.hours_by_kind.",
        "hours_by_kind.",
    ):
        if field_path.startswith(prefix):
            name = field_path[len(prefix):]
            if name:
                escaped = name.replace("~", "~0").replace("/", "~1")
                return HOURS_COMPONENT_PREFIX + escaped
    return None


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
    # Агент иногда возвращает точечную запись «attributes.hours_total» —
    # приводим её к каноническому указателю до общей обработки.
    if field_path.startswith("attributes."):
        field_path = "/attributes/" + field_path[len("attributes."):]
    compact_workload = _compact_workload_pointer(field_path)
    if compact_workload is not None:
        field_path = compact_workload
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


def workload_targets(proposal: ChangeProposal) -> set[UUID]:
    targets = set()
    for change in proposal.changes:
        if change.target_kind != "entity":
            continue
        try:
            path = normalize_field_path(change.field_path)
        except ProposalApplicationError:
            continue
        if path == HOURS_TOTAL_PATH or path.startswith(HOURS_COMPONENT_PREFIX):
            targets.add(change.target_id)
    return targets


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
        if any(link.observed_value.get("calculated") for link in links):
            errors.append("Общий объём вычисляется из отдельных граф нагрузки. Измените эти графы.")
            continue
        if field_path.startswith(HOURS_COMPONENT_PREFIX) and any(
            link.observed_value.get("cell_text") is not None for link in links
        ) and (type(change.new_value) is not int or change.new_value < 0):
            errors.append("Часы нагрузки должны быть целым неотрицательным числом")
            continue
        evidence_refs = {link.source_ref_id for link in links}
        # Агент приводит в обоснование и источник-эталон (например, строку
        # учебного плана, по которой выравниваем значение) — он не является
        # свидетельством текущего значения поля и не должен блокировать
        # применение. Отказ — только если не подтверождён ни один источник.
        if change.source_ref_ids and not (
            set(change.source_ref_ids) & evidence_refs
        ):
            errors.append(
                f"{label} {field_path}: ни один из источников "
                "не относится к свидетельствам этого поля"
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

    field = "entities" if change.target_kind == "entity" else "relations"
    members = list(getattr(snapshot, field))
    target_index = next(
        (index for index, item in enumerate(members) if item.id == change.target_id), None
    )
    if target_index is None:
        raise ProposalApplicationError(
            f"Цель правки ({change.target_kind} {change.target_id}) отсутствует в выбранной версии"
        )
    target = members[target_index]
    attributes = deepcopy(target.attributes)
    tokens = _pointer_tokens(change.field_path)[1:]
    value: object = attributes
    for token in tokens[:-1]:
        value = value[int(token)] if isinstance(value, list) else value[token]
    last = tokens[-1]
    if isinstance(value, list):
        value[int(last)] = change.new_value
    else:
        value[last] = change.new_value
    if (
        change.target_kind == "entity" and target.kind in WORKLOAD_ENTITY_KINDS
        and change.field_path.startswith(HOURS_COMPONENT_PREFIX)
    ):
        attributes["hours_total"] = sum(attributes["hours_by_kind"].values())
    members[target_index] = target.model_copy(update={"attributes": attributes})
    return snapshot.model_copy(update={field: members})


def _evidence_with_unit(snapshot: SnapshotV2, change) -> list[FieldEvidence]:
    """Записанные свидетельства одного поля с единицей измерения для поиска в документе."""
    links: list[FieldEvidence] = []
    for link in _evidence_links(
        snapshot, change.target_kind, UUID(str(change.target_id)), change.field_path
    ):
        observed = link.observed_value
        if "unit" not in observed:
            unit = _fallback_unit(change.field_path)
            if unit is not None:
                observed = {**observed, "unit": unit}
        links.append(
            FieldEvidence(
                change.target_kind,
                str(change.target_id),
                change.field_path,
                str(link.source_ref_id),
                observed,
            )
        )
    return links


def unwritable_changes(
    snapshot: SnapshotV2, changes: list[ApprovedChange], documents: Mapping[str, bytes]
) -> list[str]:
    """Изменения предложения, которые не получится записать в исходные документы.

    Проверка та же, что выполнит генерация: записанное значение должно стоять
    на своём месте ровно один раз, и замена должна применяться к документу.
    Вызывается до создания версии, чтобы пользователь узнал об отказе сразу, а
    не после подтверждения. ``documents`` — байты DOCX по идентификатору.
    """

    from app.generation_bridge import field_label
    from worker.generation.patches import (
        GenerationMappingError,
        check_patch_applies,
        plan_patches,
    )

    problems: list[str] = []
    for change in changes:
        links = _evidence_with_unit(snapshot, change)
        used = {link.source_ref_id for link in links}
        sources = [
            SourceEvidence(str(ref.id), str(ref.document_id), ref.locator)
            for ref in snapshot.source_refs
            if str(ref.id) in used
        ]
        try:
            planned = plan_patches(
                [
                    # Свидетельства хранят идентификатор текстом — ключ должен совпасть.
                    FieldChange(
                        change.target_kind,
                        str(change.target_id),
                        change.field_path,
                        change.new_value,
                    )
                ],
                links,
                sources,
            )
        except GenerationMappingError as error:
            # Список, таблица или текст с переносами вместо одного значения.
            label = field_label(change.field_path)
            problems.append(
                f"{label}: это не одно значение (список, таблица или текст с "
                "переносами) — укажите отдельное поле"
                if "нельзя записать в документ" in str(error) or "одним текстовым" in str(error)
                else f"{label}: {error}"
            )
            continue
        for document_id, patches in planned.items():
            document = documents.get(document_id)
            if document is None:
                problems.append(
                    f"{field_label(change.field_path)}: исходный документ недоступен"
                )
                continue
            for patch in patches:
                reason = check_patch_applies(document, patch)
                if reason:
                    problems.append(
                        f"{field_label(change.field_path)}: прежнее значение "
                        f"«{patch.old_text[:60]}» {reason}"
                    )
    return problems


def generation_inputs(
    snapshot: SnapshotV2, changes: list[ApprovedChange]
) -> tuple[list[FieldChange], list[FieldEvidence], list[SourceEvidence]]:
    """Вход ``build_generation_documents`` для итогового графа и новых правок.

    Текущие изменения накладываются на копию графа; итог сравнивается с
    первоначальными наблюдениями всех документов.
    """

    from app.generation_bridge import snapshot_changes

    final_snapshot = snapshot.model_copy(deep=True)
    for change in changes:
        final_snapshot = apply_change(final_snapshot, change)

    refs = {ref.id: ref for ref in snapshot.source_refs}
    field_changes = snapshot_changes(final_snapshot)
    links: list[FieldEvidence] = []
    used_refs: set[UUID] = set()
    for change in field_changes:
        for link in _evidence_with_unit(snapshot, change):
            used_refs.add(UUID(link.source_ref_id))
            links.append(link)
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
    """Документы, которых коснутся правки (для показа до применения)."""

    refs = {ref.id: ref.document_id for ref in snapshot.source_refs}
    documents: set[UUID] = set()
    for change in changes:
        change_links = _evidence_links(
            snapshot, change.target_kind, change.target_id, change.field_path
        )
        for link in change_links:
            documents.add(refs[link.source_ref_id])
    return documents
