"""Исходные ответы анализа сохраняются как неизменяемые артефакты аудита."""

import hashlib
import os
from uuid import uuid4

from app.config import get_settings
from app.graph_storage import canonical_bytes


def save_response(value):
    if value is None:
        return None
    raw = canonical_bytes(value)
    checksum = hashlib.sha256(raw).hexdigest()
    root = get_settings().artifact_files_path
    root.mkdir(parents=True, exist_ok=True)
    key = "analysis-response-" + checksum + ".json"
    final = root / key
    temporary = root / (".upload-" + uuid4().hex)
    try:
        with temporary.open("xb") as stream:
            stream.write(raw)
            stream.flush()
            os.fsync(stream.fileno())
        try:
            os.link(temporary, final)
        except FileExistsError:
            if final.read_bytes() != raw:
                raise ValueError("Содержимое артефакта аудита изменилось") from None
        directory = os.open(root, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        temporary.unlink(missing_ok=True)
    return {
        "storage_key": key,
        "sha256": checksum,
        "size_bytes": len(raw),
        "hash_encoding": "jcs-rfc8785-utf8",
    }
