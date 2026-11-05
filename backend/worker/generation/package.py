"""Build a ZIP containing generated DOCX files for supported package parts."""

from __future__ import annotations

from dataclasses import dataclass
from io import BytesIO
from pathlib import PurePosixPath
from typing import Literal, Sequence
from zipfile import BadZipFile, ZIP_DEFLATED, ZipFile


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


def _validate_member(document: GeneratedDocx) -> None:
    if document.document_type not in _SUPPORTED_TYPES:
        raise GenerationPackageError(
            f"Тип {document.document_type!r} пока не включён в генератор"
        )
    path = PurePosixPath(document.file_name)
    if (
        not document.file_name
        or path.name != document.file_name
        or "\\" in document.file_name
        or document.file_name in {".", ".."}
        or path.suffix.casefold() != ".docx"
    ):
        raise GenerationPackageError("Имя выходного файла должно быть безопасным именем *.docx")
    try:
        with ZipFile(BytesIO(document.content)) as docx:
            names = set(docx.namelist())
            if "[Content_Types].xml" not in names or "word/document.xml" not in names:
                raise GenerationPackageError(f"{document.file_name}: файл не является DOCX")
    except BadZipFile as exc:
        raise GenerationPackageError(f"{document.file_name}: файл не является корректным DOCX") from exc


def build_docx_package(documents: Sequence[GeneratedDocx]) -> bytes:
    """Return a ZIP archive of supported generated DOCX documents.

    Curriculum is supported as DOCX. The target competency matrix format is
    XLSX; matrix generation stays out of scope until its approach is clarified
    with the mentor.
    """

    if not documents:
        raise GenerationPackageError("Нельзя собрать пустой пакет документов")

    seen: set[str] = set()
    for document in documents:
        _validate_member(document)
        key = document.file_name.casefold()
        if key in seen:
            raise GenerationPackageError(f"Повтор имени выходного файла: {document.file_name}")
        seen.add(key)

    output = BytesIO()
    with ZipFile(output, "w", compression=ZIP_DEFLATED) as package:
        for document in documents:
            package.writestr(document.file_name, document.content)
    return output.getvalue()
