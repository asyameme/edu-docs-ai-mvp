"""Map approved entity changes onto per-document DOCX patch lists.

The mapping layer between the review graph and the DOCX patcher: a confirmed
attribute write (the same shape as the review ``set_attribute`` mutation) is
anchored to its parser evidence (``source_links`` → ``source_refs``), grouped
by document and resolved against the original file bytes, so the patcher
receives coordinates plus the exact on-page text to replace. Evidence that no
longer matches the document, or changes without recorded evidence, fail before
any patching starts. Only attribute writes map to text replacement; adding or
removing entities changes the document structure and stays out of scope.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from json import JSONDecodeError
from json import loads as _json_loads
from typing import Literal

from .docx_patch import DocxPatch, read_area_text
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
    documents: Mapping[str, bytes], planned: Mapping[str, Sequence[PlannedPatch]]
) -> dict[str, tuple[DocxPatch, ...]]:
    """Check every planned patch against the original bytes and freeze it.

    The recorded observed value must still occur in the area selected by the
    coordinates; otherwise the evidence is stale and patching is refused.
    Two patches claiming the same on-page fragment with different texts also
    fail here, before the patcher starts.
    """

    resolved: dict[str, tuple[DocxPatch, ...]] = {}
    for document_id, patches in planned.items():
        document = documents.get(document_id)
        if document is None:
            raise GenerationMappingError(f"В пакете нет документа {document_id}")
        claimed: dict[str, str] = {}
        frozen: list[DocxPatch] = []
        for patch in patches:
            area = read_area_text(document, patch.source_ref)
            if not any(text.count(patch.old_text) for text in area):
                raise GenerationMappingError(
                    f"В документе {document_id} не найдено записанное значение "
                    f"{patch.old_text!r} поля {_describe(patch.change)}; "
                    "свидетельство устарело относительно исходного файла"
                )
            previous = claimed.get(patch.old_text)
            if previous is not None and previous != patch.new_text:
                raise GenerationMappingError(
                    f"Два изменения претендуют на один фрагмент {patch.old_text!r} "
                    f"документа {document_id}"
                )
            claimed[patch.old_text] = patch.new_text
            frozen.append(
                DocxPatch(
                    source_ref=patch.source_ref,
                    old_text=patch.old_text,
                    new_text=patch.new_text,
                )
            )
        resolved[document_id] = tuple(frozen)
    return resolved


def build_generation_documents(
    changes: Sequence[FieldChange],
    links: Sequence[FieldEvidence],
    refs: Sequence[SourceEvidence],
    originals: Sequence[OriginalDocument],
) -> list[SourceDocx]:
    """Convert approved changes into the generator input.

    Every original document is returned: documents without changes carry no
    patches and pass through the generator byte-identical. Evidence pointing
    outside the package and stale observed values raise before any patching
    starts.
    """

    planned = plan_patches(changes, links, refs)
    original_index = {document.document_id: document for document in originals}
    missing = set(planned) - set(original_index)
    if missing:
        raise GenerationMappingError(
            "Свидетельства ссылаются на документы вне пакета: " + ", ".join(sorted(missing))
        )
    documents = {document_id: item.content for document_id, item in original_index.items()}
    resolved = resolve_planned_patches(documents, planned)
    return [
        SourceDocx(
            document_type=document.document_type,
            file_name=document.file_name,
            content=document.content,
            patches=resolved.get(document.document_id, ()),
        )
        for document in originals
    ]
