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
        name, suffix = self._validate_filename(upload.filename)
        content = upload.file.read(self.max_bytes + 1)
        return self._save_content(name, suffix, content)

    def save_bytes(self, filename, content, *, precomputed_sha256=None):
        """Подготавливает оригинал без промежуточного файла и повторного чтения."""
        name, suffix = self._validate_filename(filename)
        return self._save_content(name, suffix, content, precomputed_sha256)

    @staticmethod
    def _validate_filename(filename):
        name = (filename or "").replace("\\", "/").rsplit("/", 1)[-1]
        if not name or any(ord(c) < 32 for c in name) or len(name) > 500:
            raise DomainError(422, "Invalid original filename")
        suffix = Path(name).suffix.lower().lstrip(".")
        if suffix not in MEDIA_TYPES:
            raise DomainError(415, "Supported formats: PDF, DOCX, XLSX")
        return name, suffix

    def _save_content(self, name, suffix, content, precomputed_sha256=None):
        if len(content) > self.max_bytes:
            raise DomainError(413, "File exceeds configured size limit")
        if not content:
            raise DomainError(422, "File is empty")
        digest = precomputed_sha256 or hashlib.sha256(content).hexdigest()
        return {
            "id": uuid4(),
            "content": content,
            "original_name": name,
            "format": suffix,
            "media_type": MEDIA_TYPES[suffix],
            "size_bytes": len(content),
            "sha256": digest,
        }
