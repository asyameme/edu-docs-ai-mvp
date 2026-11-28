"""Generate program packages by patching source DOCX documents."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from backend.contracts.document_v2.graph import EducationalGraph

from .docx_patch import (
    DocxPatch,
    DocxPatchError,
    RowInsertion,
    RowRemoval,
    insert_table_rows,
    patch_docx_bytes,
    remove_table_rows,
)
from .matrix_xlsx import build_matrix_xlsx
from .package import (
    DocumentType,
    GeneratedDocx,
    GeneratedXlsx,
    build_docx_package,
    build_program_package,
)


@dataclass(frozen=True, slots=True)
class SourceDocx:
    """Original program document and its approved changes.

    Text ``patches`` replace values in place. ``row_removals`` delete whole
    table rows (a competency taken out of a discipline) after the text patches,
    and ``row_insertions`` append new rows (a competency added to a discipline),
    all against the coordinates of the original document.
    """

    document_type: DocumentType
    file_name: str
    content: bytes
    patches: tuple[DocxPatch, ...] = ()
    row_removals: tuple[RowRemoval, ...] = ()
    row_insertions: tuple[RowInsertion, ...] = ()
    # Идентификатор исходного документа пакета; нужен, чтобы добавить правки
    # к уже собранному документу (with_document_changes).
    document_id: str = ""


def _patch_document(source: SourceDocx, notes: list[str] | None) -> tuple[bytes, int]:
    """Apply the text patches of one document, keeping the rest if one fails.

    Without ``notes`` any failure stops the generation. With it, a document that
    fails as a whole is patched one change at a time and every change that the
    patcher refuses (text spanning differently formatted fragments, for example)
    is reported and left for a manual edit instead of blocking the package.
    """

    patches = tuple(patch for patch in source.patches if patch.old_text != patch.new_text)
    try:
        return patch_docx_bytes(source.content, patches), len(patches)
    except DocxPatchError:
        if notes is None:
            raise
    content = source.content
    written = 0
    for patch in patches:
        try:
            content = patch_docx_bytes(content, [patch])
            written += 1
        except DocxPatchError as patch_error:
            notes.append(
                f"{source.file_name}: не заменено «{patch.old_text[:60]}» на "
                f"«{patch.new_text[:60]}» ({patch_error}) — исправьте вручную"
            )
    return content, written


def _render_docx_documents(
    documents: Sequence[SourceDocx],
    notes: list[str] | None = None,
    applied: list[str] | None = None,
) -> list[GeneratedDocx]:
    generated: list[GeneratedDocx] = []
    for source in documents:
        content, replaced = _patch_document(source, notes)
        removed = 0
        if source.row_removals:
            removals = {
                (item.source_ref.get("table"), item.source_ref.get("row")): item
                for item in source.row_removals
            }
            content, kept = remove_table_rows(content, tuple(removals.values()))
            removed = len(removals) - len(kept)
            if kept and notes is None:
                raise DocxPatchError("; ".join(kept))
            if notes is not None:
                notes.extend(
                    f"{source.file_name}: строка не удалена ({item}) — исправьте вручную"
                    for item in kept
                )
        inserted = 0
        if source.row_insertions:
            unique = {
                (item.source_ref.get("table"), item.cells): item
                for item in source.row_insertions
            }
            try:
                content = insert_table_rows(content, tuple(unique.values()))
                inserted = len(unique)
            except DocxPatchError as error:
                if notes is None:
                    raise
                notes.append(
                    f"{source.file_name}: строка не добавлена ({error}) — исправьте вручную"
                )
        if applied is not None and (replaced or removed or inserted):
            parts = []
            if replaced:
                parts.append(f"заменено значений: {replaced}")
            if removed:
                parts.append(f"удалено строк таблиц: {removed}")
            if inserted:
                parts.append(f"добавлено строк таблиц: {inserted}")
            applied.append(f"{source.file_name} — " + ", ".join(parts))
        generated.append(
            GeneratedDocx(
                document_type=source.document_type,
                file_name=source.file_name,
                content=content,
            )
        )
    return generated


def generate_docx_package(documents: Sequence[SourceDocx]) -> bytes:
    """Return a ZIP with updated DOCX files and all unchanged source DOCX files.

    The input is the confirmed source package plus an explicit list of text
    changes per document. Unchanged documents are copied through as-is; changed
    documents retain their original layout and package parts.
    """

    if not documents:
        raise ValueError("Нельзя сгенерировать пакет без исходных документов")

    return build_docx_package(_render_docx_documents(documents))


def generate_program_package(
    documents: Sequence[SourceDocx],
    graph: EducationalGraph,
    matrix_file_name: str = "Матрица компетенций.xlsx",
    notes: list[str] | None = None,
    *,
    applied: list[str] | None = None,
    unresolved_pairs=(),
) -> bytes:
    """Generate patched source DOCX files and a derived matrix in one ZIP.

    The matrix is derived from the current normalized graph, so it reflects
    graph changes included in this generation run. The source package remains
    the base for DOCX patching; no input matrix file is needed. ``notes``, when
    given, collects the changes that could not be written into a document.
    """

    if not documents:
        raise ValueError("Нельзя сгенерировать пакет без исходных документов")

    generated = _render_docx_documents(documents, notes, applied)
    generated.append(
        GeneratedXlsx(
            file_name=matrix_file_name,
            content=build_matrix_xlsx(graph, unresolved_pairs=unresolved_pairs),
        )
    )
    return build_program_package(generated, manual_notes=notes or ())
