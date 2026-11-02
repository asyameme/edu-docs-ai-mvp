"""Подготовка исходных байтов для атомарной записи с метаданными в PostgreSQL."""

import hashlib
from pathlib import Path
from uuid import uuid4

from app.errors import DomainError

MEDIA_TYPES = {
    "pdf": "application/pdf",
    "docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    "xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
}


class OriginalStorage:
    def __init__(self, max_bytes: int):
        self.max_bytes = max_bytes

    def save(self, upload):
        name = (upload.filename or "").replace("\\", "/").rsplit("/", 1)[-1]
        if not name or any(ord(c) < 32 for c in name) or len(name) > 500:
            raise DomainError(422, "Invalid original filename")
        suffix = Path(name).suffix.lower().lstrip(".")
        if suffix not in MEDIA_TYPES:
            raise DomainError(415, "Supported formats: PDF, DOCX, XLSX")
        # Читаем только разрешённый объём и один байт для проверки превышения.
        content = upload.file.read(self.max_bytes + 1)
        if len(content) > self.max_bytes:
            raise DomainError(413, "File exceeds configured size limit")
        if not content:
            raise DomainError(422, "File is empty")
        return {
            "id": uuid4(),
            "content": content,
            "original_name": name,
            "format": suffix,
            "media_type": MEDIA_TYPES[suffix],
            "size_bytes": len(content),
            "sha256": hashlib.sha256(content).hexdigest(),
        }
