"""Проверка ZIP в памяти: пути из архива никогда не используются как пути на диске."""

import hashlib
import io
import lzma
import stat
import zipfile
import zlib
from dataclasses import dataclass
from pathlib import PurePosixPath

from app.errors import DomainError


@dataclass
class PackageFile:
    name: str
    content: bytes
    sha256: str


def read_package(upload, max_bytes: int) -> tuple[str, list[PackageFile]]:
    if not (upload.filename or "").lower().endswith(".zip"):
        raise DomainError(415, "Загрузите ZIP-архив с DOCX/XLSX")
    data = upload.file.read(max_bytes + 1)
    if len(data) > max_bytes:
        raise DomainError(413, "Архив превышает допустимый размер")
    files, names, hashes = [], set(), set()
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            members = archive.infolist()
            if len(members) > 100 or sum(m.file_size for m in members) > 100 * 1024 * 1024:
                raise DomainError(413, "Слишком большой распакованный пакет")
            for member in members:
                name = member.filename
                path = PurePosixPath(name)
                if (
                    not name
                    or member.orig_filename != name
                    or len(name) > 500
                    or "\\" in name
                    or ":" in name
                    or path.is_absolute()
                    or ".." in path.parts
                    or any(ord(c) < 32 for c in name)
                    or stat.S_ISLNK(member.external_attr >> 16)
                    or member.flag_bits & 1
                ):
                    raise DomainError(422, "Недопустимый путь или тип записи в архиве")
                if member.is_dir():
                    continue
                if path.suffix.lower() not in {".docx", ".xlsx"}:
                    suffix = path.suffix or "без расширения"
                    raise DomainError(422, f"Неподдерживаемый формат: {suffix}")
                if member.file_size == 0:
                    raise DomainError(422, f"Файл пуст: {name}")
                if member.file_size > max_bytes or len(files) >= 50:
                    raise DomainError(413, "Превышен размер файла или число документов (50)")
                if name.casefold() in names:
                    raise DomainError(422, "Повторяющееся имя в архиве")
                content = archive.read(member)
                digest = hashlib.sha256(content).hexdigest()
                if digest in hashes:
                    raise DomainError(422, "Один документ включён в пакет несколько раз")
                names.add(name.casefold())
                hashes.add(digest)
                files.append(PackageFile(name, content, digest))
    except (
        zipfile.BadZipFile,
        RuntimeError,
        OSError,
        EOFError,
        NotImplementedError,
        lzma.LZMAError,
        zlib.error,
    ) as error:
        raise DomainError(422, "Архив повреждён или не поддерживается") from error
    if not files:
        raise DomainError(422, "В архиве нет документов DOCX/XLSX")
    fingerprint = hashlib.sha256("\n".join(sorted(hashes)).encode()).hexdigest()
    return fingerprint, files
