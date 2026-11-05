"""Generate a DOCX-only package by patching the approved source documents."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Sequence

from .docx_patch import DocxPatch, patch_docx_bytes
from .package import DocumentType, GeneratedDocx, build_docx_package


@dataclass(frozen=True, slots=True)
class SourceDocx:
    """Original program document and its approved text changes."""

    document_type: DocumentType
    file_name: str
    content: bytes
    patches: tuple[DocxPatch, ...] = ()


def generate_docx_package(documents: Sequence[SourceDocx]) -> bytes:
    """Return a ZIP with updated DOCX files and all unchanged source DOCX files.

    The input is the confirmed source package plus an explicit list of text
    changes per document. Unchanged documents are copied through as-is; changed
    documents retain their original layout and package parts. Curriculum is
    supported as DOCX. The target matrix output format is XLSX; matrix generation
    remains excluded until its generation approach is clarified with the mentor.
    """

    if not documents:
        raise ValueError("Нельзя сгенерировать пакет без исходных документов")

    generated: list[GeneratedDocx] = []
    for source in documents:
        rendered = patch_docx_bytes(source.content, source.patches)
        generated.append(
            GeneratedDocx(
                document_type=source.document_type,
                file_name=source.file_name,
                content=rendered,
            )
        )
    return build_docx_package(generated)
