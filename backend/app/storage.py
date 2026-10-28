"""Хранение исходных байтов; расширение обозначает формат, а не успешный разбор."""

import hashlib
import os
from pathlib import Path
from uuid import UUID, uuid4

from app.errors import DomainError

MEDIA_TYPES = {
    "pdf": "application/pdf",
    "docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    "xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
}


class OriginalStorage:
    def __init__(self, root: Path, max_bytes: int):
        self.root = root
        self.max_bytes = max_bytes

    def path(self, key: str) -> Path:
        # Ключ в БД — UUID, созданный сервером, а не пользовательское имя файла.
        return self.root / str(UUID(key))

    def save(self, upload):
        name = (upload.filename or "").replace("\\", "/").rsplit("/", 1)[-1]
        if not name or any(ord(c) < 32 for c in name) or len(name) > 500:
            raise DomainError(422, "Invalid original filename")
        suffix = Path(name).suffix.lower().lstrip(".")
        if suffix not in MEDIA_TYPES:
            raise DomainError(415, "Supported formats: PDF, DOCX, XLSX")
        document_id = uuid4()
        key = str(document_id)
        final = self.path(key)
        temporary = self.root / (".upload-" + key)
        digest, size = hashlib.sha256(), 0
        try:
            self.root.mkdir(parents=True, exist_ok=True)
            with temporary.open("xb") as target:
                while chunk := upload.file.read(1024 * 1024):
                    size += len(chunk)
                    if size > self.max_bytes:
                        raise DomainError(413, "File exceeds configured size limit")
                    digest.update(chunk)
                    target.write(chunk)
                if not size:
                    raise DomainError(422, "File is empty")
                target.flush()
                os.fsync(target.fileno())
            os.replace(temporary, final)
        except BaseException:
            temporary.unlink(missing_ok=True)
            raise
        return {
            "id": document_id,
            "storage_key": key,
            "original_name": name,
            "format": suffix,
            "media_type": MEDIA_TYPES[suffix],
            "size_bytes": size,
            "sha256": digest.hexdigest(),
        }

    def remove(self, key):
        self.path(key).unlink(missing_ok=True)

    def download_path(self, key):
        path = self.path(key)
        if not path.is_file():
            raise DomainError(503, "Original file is unavailable")
        return path
