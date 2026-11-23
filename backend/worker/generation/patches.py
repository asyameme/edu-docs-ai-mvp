"""Map approved entity changes onto per-document DOCX patch lists.

The mapping layer between the review graph and the DOCX patcher: a confirmed
attribute write (the same shape as the review ``set_attribute`` mutation) is
anchored to its parser evidence (``source_links`` → ``source_refs``), grouped
by document and resolved against the original file bytes, so the patcher
receives coordinates plus the exact on-page text to replace. Evidence that no
longer matches the document, or changes without recorded evidence, fail before
any patching starts. Only attribute writes map to text replacement; adding or
removing entities changes the document structure and stays out of scope —
except competency lists, which ``generation_bridge`` maps onto table rows.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from json import JSONDecodeError
from json import dumps as _json_dumps
from json import loads as _json_loads
from typing import Literal

from .docx_patch import (
    DocxPatch,
    DocxPatchError,
    RowRemoval,
    patch_docx_bytes,
    read_area_text,
)
from .generator import SourceDocx
from .package import DocumentType

TargetKind = Literal["entity", "relation"]
_LOCATOR_KEYS = ("section", "paragraph_index", "table", "row", "col")


class GenerationMappingError(ValueError):
    """Raised when approved changes cannot be mapped onto source documents."""


@dataclass(frozen=True, slots=True)
class FieldChange:
    """One approved attribute write.

    ``field_path`` is a JSON Pointer inside the object, e.g.
    ``/attributes/hours``; ``new_value`` is the approved new field value.
    """

    target_kind: TargetKind
    target_id: str
    field_path: str
    new_value: object


@dataclass(frozen=True, slots=True)
class FieldEvidence:
    """One ``source_links`` row: a field observed at a recorded source."""

    target_kind: TargetKind
    target_id: str
    field_path: str
    source_ref_id: str
    observed_value: Mapping[str, object]


@dataclass(frozen=True, slots=True)
class SourceEvidence:
    """One ``source_refs`` row: parser coordinates inside one document."""

    ref_id: str
    document_id: str
    locator: str


@dataclass(frozen=True, slots=True)
class OriginalDocument:
    """A package document with its immutable original bytes."""

    document_id: str
    document_type: DocumentType
    file_name: str
    content: bytes


@dataclass(frozen=True, slots=True)
class PlannedPatch:
    """A patch resolved to a document but not yet to the on-page text."""

    document_id: str
    source_ref: dict[str, object]
    old_text: str
    new_text: str
    change: FieldChange


@dataclass(frozen=True, slots=True)
class UnresolvedPatch:
    """A planned patch that will not be written into its document.

    ``occurrences`` is 0 when the recorded value is missing from its location,
    above 1 when it is found there more than once, and ``CONFLICT`` when other
    changes demand a different text for the same place.
    """

    document_id: str
    patch: PlannedPatch
    occurrences: int
    reason: str | None = None


# Несколько изменений требуют разных текстов в одном месте документа.
CONFLICT = -1


def _describe(change: FieldChange) -> str:
    return f"{change.target_kind} {change.target_id} {change.field_path}"


def _render_value(value: object, unit: object = None) -> str:
    """Render a field value as the exact document text that represents it.

    When the evidence records a unit, it is appended for every value kind:
    the unit describes how the fact appears on the page. Values without a
    unit render as their plain text form. ``None`` and booleans have no
    unambiguous text form and are rejected.
    """

    if value is None:
        raise GenerationMappingError("Пустое значение нельзя записать в документ автоматически")
    if isinstance(value, bool):
        raise GenerationMappingError("Булево значение нельзя записать в документ без шаблона поля")
    if isinstance(value, int):
        rendered = str(value)
    elif isinstance(value, float):
        rendered = str(int(value)) if value.is_integer() else str(value)
    elif isinstance(value, str):
        rendered = value
    else:
        raise GenerationMappingError(
            f"Значение типа {type(value).__name__} нельзя записать в документ"
        )
    if any(character in rendered for character in "\r\n\t"):
        raise GenerationMappingError("Значение должно быть одним текстовым фрагментом")
    if not rendered:
        raise GenerationMappingError("Пустое значение нельзя записать в документ автоматически")
    if unit is not None:
        if not isinstance(unit, str) or not unit.strip():
            raise GenerationMappingError("Единица измерения должна быть непустой строкой")
        rendered = f"{rendered} {unit.strip()}"
    return rendered


def _parse_locator(locator: str) -> dict[str, object]:
    try:
        coordinates = _json_loads(locator)
    except (TypeError, JSONDecodeError) as exc:
        raise GenerationMappingError("source_refs.locator не является корректным JSON") from exc
    if not isinstance(coordinates, dict):
        raise GenerationMappingError("source_refs.locator должен быть JSON-объектом координат")
    return {key: coordinates[key] for key in _LOCATOR_KEYS if key in coordinates}


def _patch_location_key(source_ref: Mapping[str, object]) -> str:
    """Return the coordinates that actually select the patcher's search area."""

    if source_ref.get("table") is not None:
        coordinates = {key: source_ref[key] for key in ("table", "row", "col") if key in source_ref}
    elif source_ref.get("paragraph_index") is not None:
        coordinates = {"paragraph_index": source_ref["paragraph_index"]}
    else:
        coordinates = {}
    return _json_dumps(coordinates, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def plan_patches(
    changes: Sequence[FieldChange], links: Sequence[FieldEvidence], refs: Sequence[SourceEvidence]
) -> dict[str, list[PlannedPatch]]:
    """Anchor every approved change to its recorded evidence, grouped by document.

    One field may carry several evidence rows when the same fact appears in
    several documents; each row yields its own patch. Duplicate identical
    changes collapse into one; conflicting writes to the same field fail.
    """

    ref_index = {ref.ref_id: ref for ref in refs}
    link_index: dict[tuple[str, str, str], list[FieldEvidence]] = defaultdict(list)
    for link in links:
        link_index[(link.target_kind, link.target_id, link.field_path)].append(link)

    planned: dict[str, list[PlannedPatch]] = defaultdict(list)
    seen: dict[tuple[str, str, str], FieldChange] = {}
    for change in changes:
        key = (change.target_kind, change.target_id, change.field_path)
        previous = seen.get(key)
        if previous is not None:
            if previous.new_value != change.new_value:
                raise GenerationMappingError(
                    f"Противоречивые изменения одного поля: {_describe(change)}"
                )
            continue
        seen[key] = change

        evidence = link_index.get(key)
        if not evidence:
            raise GenerationMappingError(
                f"Для изменения нет записанных свидетельств источника: {_describe(change)}"
            )
        for link in evidence:
            ref = ref_index.get(link.source_ref_id)
            if ref is None:
                raise GenerationMappingError(
                    f"Свидетельство поля {_describe(change)} ссылается "
                    f"на неизвестный source_ref {link.source_ref_id}"
                )
            if "value" not in link.observed_value:
                raise GenerationMappingError(
                    f"В свидетельстве поля {_describe(change)} нет наблюдаемого значения"
                )
            unit = link.observed_value.get("unit")
            try:
                old_text = _render_value(link.observed_value["value"], unit)
                new_text = _render_value(change.new_value, unit)
            except GenerationMappingError as error:
                raise GenerationMappingError(
                    f"Свидетельство поля {_describe(change)}: {error}"
                ) from None
            planned[ref.document_id].append(
                PlannedPatch(
                    document_id=ref.document_id,
                    source_ref=_parse_locator(ref.locator),
                    old_text=old_text,
                    new_text=new_text,
                    change=change,
                )
            )
    return dict(planned)


def resolve_planned_patches(
    documents: Mapping[str, bytes],
    planned: Mapping[str, Sequence[PlannedPatch]],
    unresolved: list[UnresolvedPatch] | None = None,
) -> dict[str, tuple[DocxPatch, ...]]:
    """Check every planned patch against the original bytes and freeze it.

    The recorded observed value must still occur in the area selected by the
    coordinates; otherwise the evidence is stale and patching is refused. Two
    patches claiming the same source location and old text with different
    replacements fail here. Identical claims collapse to one patch.

    With ``unresolved`` given, a value missing from its location no longer
    fails the whole plan: the patch is reported there and left out.
    """

    resolved: dict[str, tuple[DocxPatch, ...]] = {}
    for document_id, patches in planned.items():
        document = documents.get(document_id)
        if document is None:
            raise GenerationMappingError(f"В пакете нет документа {document_id}")
        frozen: list[DocxPatch] = []
        owners: list[PlannedPatch] = []
        claimed: dict[tuple[str, str], int] = {}
        dropped: set[int] = set()
        conflicted: set[tuple[str, str]] = set()
        for patch in patches:
            try:
                source_ref, occurrences = locate_observed_value(document, patch)
            except DocxPatchError as error:
                if unresolved is None:
                    raise
                unresolved.append(UnresolvedPatch(document_id, patch, 0, str(error)))
                continue
            if occurrences != 1:
                if unresolved is not None:
                    unresolved.append(UnresolvedPatch(document_id, patch, occurrences))
                    continue
                if occurrences == 0:
                    raise GenerationMappingError(
                        f"В документе {document_id} не найдено записанное значение "
                        f"{patch.old_text!r} поля {_describe(patch.change)}; "
                        "свидетельство устарело относительно исходного файла"
                    )
                raise GenerationMappingError(
                    f"В документе {document_id} значение {patch.old_text!r} "
                    f"найдено неоднозначно в области источника поля {_describe(patch.change)}"
                )
            claim_key = (_patch_location_key(source_ref), patch.old_text)
            previous = claimed.get(claim_key)
            conflicts = claim_key in conflicted or (
                previous is not None and frozen[previous].new_text != patch.new_text
            )
            if conflicts:
                if unresolved is None:
                    raise GenerationMappingError(
                        f"Два изменения претендуют на один фрагмент {patch.old_text!r} "
                        f"в одном источнике документа {document_id}"
                    )
                # В одно место документа просят записать разные тексты: не пишем
                # ни один и отдаём оба решения в отчёт.
                if previous is not None:
                    unresolved.append(UnresolvedPatch(document_id, owners[previous], CONFLICT))
                    dropped.add(previous)
                    del claimed[claim_key]
                conflicted.add(claim_key)
                unresolved.append(UnresolvedPatch(document_id, patch, CONFLICT))
                continue
            if previous is not None:
                continue
            claimed[claim_key] = len(frozen)
            owners.append(patch)
            frozen.append(
                DocxPatch(
                    source_ref=source_ref,
                    old_text=patch.old_text,
                    new_text=patch.new_text,
                )
            )
        resolved[document_id] = tuple(
            item for index, item in enumerate(frozen) if index not in dropped
        )
    return resolved


def locate_observed_value(document: bytes, patch: PlannedPatch) -> tuple[dict, int]:
    """Find where the recorded value occurs for one planned patch.

    The parser addresses a record by one cell, but the value itself may sit in a
    neighbouring cell of the same table row (a formulation next to its code).
    The recorded cell is checked first; when it does not contain the value and
    the whole row contains it exactly once, the row becomes the patch location.
    """
    source_ref = patch.source_ref
    occurrences = sum(text.count(patch.old_text) for text in read_area_text(document, source_ref))
    if occurrences == 0 and source_ref.get("col") is not None:
        row_ref = {key: value for key, value in source_ref.items() if key != "col"}
        row_area = read_area_text(document, row_ref)
        if sum(text.count(patch.old_text) for text in row_area) == 1:
            return row_ref, 1
    return dict(source_ref), occurrences


def check_patch_applies(document: bytes, patch: PlannedPatch) -> str | None:
    """Return why one planned patch cannot be applied to ``document``, or None.

    This is the strict check used before an edit is accepted: the recorded value
    must be found exactly once at its location, and the replacement itself must
    succeed on a scratch copy (text spanning differently formatted fragments is
    refused by the patcher).
    """

    try:
        source_ref, occurrences = locate_observed_value(document, patch)
        if occurrences == 0:
            return "не найдено в документе"
        if occurrences > 1:
            return "встречается в источнике несколько раз"
        patch_docx_bytes(
            document,
            [
                DocxPatch(
                    source_ref=source_ref, old_text=patch.old_text, new_text=patch.new_text
                )
            ],
        )
    except DocxPatchError as error:
        return str(error)
    return None


def build_generation_documents(
    changes: Sequence[FieldChange],
    links: Sequence[FieldEvidence],
    refs: Sequence[SourceEvidence],
    originals: Sequence[OriginalDocument],
    unresolved: list[UnresolvedPatch] | None = None,
) -> list[SourceDocx]:
    """Convert approved changes into the generator input.

    Every original document is returned: documents without changes carry no
    patches and pass through the generator byte-identical. Evidence pointing
    outside the package raises before any patching starts; a stale observed
    value raises too, unless ``unresolved`` is given to collect it.
    """

    planned = plan_patches(changes, links, refs)
    original_index = {document.document_id: document for document in originals}
    missing = set(planned) - set(original_index)
    if missing:
        raise GenerationMappingError(
            "Свидетельства ссылаются на документы вне пакета: " + ", ".join(sorted(missing))
        )
    documents = {document_id: item.content for document_id, item in original_index.items()}
    resolved = resolve_planned_patches(documents, planned, unresolved)
    return [
        SourceDocx(
            document_type=document.document_type,
            file_name=document.file_name,
            content=document.content,
            patches=resolved.get(document.document_id, ()),
            document_id=document.document_id,
        )
        for document in originals
    ]


def with_document_changes(
    documents: Sequence[SourceDocx],
    document_id: str,
    *,
    patches: Sequence[DocxPatch] = (),
    row_removals: Sequence[RowRemoval] = (),
) -> list[SourceDocx]:
    """Return the documents with extra changes added to one of them.

    Text patches and row removals of the same document are applied together:
    patches first, then the removals, both against the original coordinates.
    """

    return [
        replace(
            document,
            patches=document.patches + tuple(patches),
            row_removals=document.row_removals + tuple(row_removals),
        )
        if document.document_id == document_id
        else document
        for document in documents
    ]
