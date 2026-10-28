"""Сверка без изменения данных; не удаляет файлы, включая незавершённые загрузки."""

import json
from uuid import UUID

from psycopg_pool import ConnectionPool

from app.config import get_settings


def main():
    settings = get_settings()
    with ConnectionPool(
        kwargs={
            "host": settings.postgres_host,
            "dbname": settings.postgres_db,
            "user": settings.postgres_user,
            "password": settings.postgres_password.get_secret_value(),
        }
    ) as pool:
        with pool.connection() as connection:
            keys = {
                row[0] for row in connection.execute("SELECT storage_key FROM source_documents")
            }
        root = settings.original_files_path
        files = {p.name for p in root.iterdir() if p.is_file()} if root.exists() else set()
        originals = set()
        for name in files:
            try:
                if str(UUID(name)) == name:
                    originals.add(name)
            except ValueError:
                continue
        print(
            json.dumps(
                {
                    "missing_originals": sorted(keys - originals),
                    "unreferenced_originals": sorted(originals - keys),
                    "temporary_uploads": sorted(n for n in files if n.startswith(".upload-")),
                }
            )
        )


if __name__ == "__main__":
    main()
