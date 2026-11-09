"""Генерация выходных документов пакета программы."""

from .docx_patch import DocxPatch, DocxPatchError, patch_docx_bytes, read_area_text
from .generator import SourceDocx, generate_docx_package
from .matrix_xlsx import (
    DerivedMatrix,
    MatrixRow,
    build_matrix_xlsx,
    derive_matrix,
    read_matrix_xlsx,
)
from .package import GeneratedDocx, GenerationPackageError, build_docx_package
from .patches import (
    FieldChange,
    FieldEvidence,
    GenerationMappingError,
    OriginalDocument,
    PlannedPatch,
    SourceEvidence,
    build_generation_documents,
    plan_patches,
    resolve_planned_patches,
)

__all__ = [
    "DerivedMatrix",
    "DocxPatch",
    "DocxPatchError",
    "FieldChange",
    "FieldEvidence",
    "GeneratedDocx",
    "GenerationMappingError",
    "GenerationPackageError",
    "MatrixRow",
    "OriginalDocument",
    "PlannedPatch",
    "SourceDocx",
    "SourceEvidence",
    "build_docx_package",
    "build_generation_documents",
    "build_matrix_xlsx",
    "derive_matrix",
    "generate_docx_package",
    "patch_docx_bytes",
    "plan_patches",
    "read_area_text",
    "read_matrix_xlsx",
    "resolve_planned_patches",
]
