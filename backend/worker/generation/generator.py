"""Generate program packages by patching source DOCX documents."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from backend.contracts.document_v2.graph import EducationalGraph

from .docx_patch import DocxPatch, patch_docx_bytes
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
    """Original program document and its approved text changes."""

    document_type: DocumentType
    file_name: str
    content: bytes
    patches: tuple[DocxPatch, ...] = ()


def _render_docx_documents(documents: Sequence[SourceDocx]) -> list[GeneratedDocx]:
    generated: list[GeneratedDocx] = []
    for source in documents:
        generated.append(
            GeneratedDocx(
                document_type=source.document_type,
                file_name=source.file_name,
                content=patch_docx_bytes(source.content, source.patches),
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
) -> bytes:
    """Generate patched source DOCX files and a derived matrix in one ZIP.

    The matrix is derived from the current normalized graph, so it reflects
    graph changes included in this generation run. The source package remains
    the base for DOCX patching; no input matrix file is needed.
    """

    if not documents:
        raise ValueError("Нельзя сгенерировать пакет без исходных документов")

    generated = _render_docx_documents(documents)
    generated.append(
        GeneratedXlsx(file_name=matrix_file_name, content=build_matrix_xlsx(graph))
    )
    return build_program_package(generated)
