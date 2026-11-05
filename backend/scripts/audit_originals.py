"""Проверка исходных байтов и метаданных в PostgreSQL без изменения данных."""

import json

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
            count, invalid = connection.execute("""SELECT count(*),
                coalesce(jsonb_agg(id) FILTER(WHERE content IS NULL
                    OR octet_length(content)<>size_bytes
                    OR encode(sha256(content),'hex')<>sha256),'[]'::jsonb)
                FROM source_documents""").fetchone()
        print(json.dumps({"documents_checked": count, "invalid_originals": invalid}, default=str))
        if invalid:
            raise SystemExit(1)


if __name__ == "__main__":
    main()
