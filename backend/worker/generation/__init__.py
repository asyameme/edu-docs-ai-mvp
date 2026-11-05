"""DOCX generation helpers for approved document types."""

from .docx_patch import DocxPatch, DocxPatchError, patch_docx_bytes
from .generator import SourceDocx, generate_docx_package
from .package import GeneratedDocx, GenerationPackageError, build_docx_package

__all__ = [
    "DocxPatch",
    "DocxPatchError",
    "GeneratedDocx",
    "GenerationPackageError",
    "SourceDocx",
    "build_docx_package",
    "generate_docx_package",
    "patch_docx_bytes",
]
