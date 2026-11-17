"""Build a ZIP containing generated program documents."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from io import BytesIO
from pathlib import PurePosixPath
from typing import Literal
from zipfile import ZIP_DEFLATED, BadZipFile, ZipFile

DocumentType = Literal["RPD", "RPP", "CURRICULUM", "CKMV", "CHARACTERISTIC"]
_SUPPORTED_TYPES = {"RPD", "RPP", "CURRICULUM", "CKMV", "CHARACTERISTIC"}


class GenerationPackageError(ValueError):
    """Raised when a generated package contains an unsupported or invalid file."""


@dataclass(frozen=True, slots=True)
class GeneratedDocx:
    """One rendered DOCX member of the program package."""

    document_type: DocumentType
    file_name: str
    content: bytes


@dataclass(frozen=True, slots=True)
class GeneratedXlsx:
    """One generated XLSX member of the program package."""

    file_name: str
    content: bytes


GeneratedPackageFile = GeneratedDocx | GeneratedXlsx


def _validate_file_name(file_name: str, suffix: str) -> None:
    path = PurePosixPath(file_name)
    if (
        not file_name
        or path.name != file_name
        or "\\" in file_name
        or file_name in {".", ".."}
        or path.suffix.casefold() != suffix
    ):
        raise GenerationPackageError(
            f"Имя выходного файла должно быть безопасным именем *{suffix}"
        )


def _validate_docx(document: GeneratedDocx) -> None:
    if document.document_type not in _SUPPORTED_TYPES:
        raise GenerationPackageError(
            f"Тип {document.document_type!r} пока не включён в генератор"
        )
    _validate_file_name(document.file_name, ".docx")
    try:
        with ZipFile(BytesIO(document.content)) as docx:
            names = set(docx.namelist())
            if "[Content_Types].xml" not in names or "word/document.xml" not in names:
                raise GenerationPackageError(f"{document.file_name}: файл не является DOCX")
    except BadZipFile as exc:
        raise GenerationPackageError(
            f"{document.file_name}: файл не является корректным DOCX"
        ) from exc


def _validate_xlsx(document: GeneratedXlsx) -> None:
    _validate_file_name(document.file_name, ".xlsx")
    try:
        with ZipFile(BytesIO(document.content)) as xlsx:
            names = set(xlsx.namelist())
            if "[Content_Types].xml" not in names or "xl/workbook.xml" not in names:
                raise GenerationPackageError(f"{document.file_name}: файл не является XLSX")
    except BadZipFile as exc:
        raise GenerationPackageError(f"{document.file_name}: файл не является корректным XLSX") from exc


def build_program_package(documents: Sequence[GeneratedPackageFile]) -> bytes:
    """Return a ZIP archive containing generated DOCX and XLSX documents."""

    if not documents:
        raise GenerationPackageError("Нельзя собрать пустой пакет документов")

    seen: set[str] = set()
    for document in documents:
        if isinstance(document, GeneratedDocx):
            _validate_docx(document)
        elif isinstance(document, GeneratedXlsx):
            _validate_xlsx(document)
        else:
            raise GenerationPackageError("Неподдерживаемый тип выходного файла")
        key = document.file_name.casefold()
        if key in seen:
            raise GenerationPackageError(f"Повтор имени выходного файла: {document.file_name}")
        seen.add(key)

    output = BytesIO()
    with ZipFile(output, "w", compression=ZIP_DEFLATED) as package:
        for document in documents:
            package.writestr(document.file_name, document.content)
    return output.getvalue()


def build_docx_package(documents: Sequence[GeneratedDocx]) -> bytes:
    """Return a ZIP archive of supported generated DOCX documents.

    This DOCX-only entry point remains for callers that do not generate the
    derived competency matrix.
    """

    if not documents:
        raise GenerationPackageError("Нельзя собрать пустой пакет документов")
    if any(not isinstance(document, GeneratedDocx) for document in documents):
        raise GenerationPackageError("DOCX-пакет может содержать только файлы DOCX")
    return build_program_package(documents)
