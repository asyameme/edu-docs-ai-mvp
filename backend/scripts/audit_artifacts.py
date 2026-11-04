"""Проверка Markdown-тома и SQL-метаданных без удаления файлов."""

import hashlib
import json
from uuid import UUID

import psycopg

from app.config import get_settings


def main():
    settings = get_settings()
    with psycopg.connect(
        host=settings.postgres_host,
        dbname=settings.postgres_db,
        user=settings.postgres_user,
        password=settings.postgres_password.get_secret_value(),
    ) as connection:
        expected = {
            str(UUID(key)): digest
            for key, digest in connection.execute(
                "SELECT storage_key,sha256 FROM document_artifacts"
            )
        }
    root = settings.artifact_files_path
    present = {path.name for path in root.iterdir() if path.is_file()} if root.exists() else set()
    corrupted = [
        key
        for key in expected
        if key in present and hashlib.sha256((root / key).read_bytes()).hexdigest() != expected[key]
    ]
    print(
        json.dumps(
            {
                "missing_artifacts": sorted(set(expected) - present),
                "corrupted_artifacts": sorted(corrupted),
                "unreferenced_artifacts": sorted(present - set(expected)),
            }
        )
    )


if __name__ == "__main__":
    main()
