"""Сверка производного Neo4j с SQL без удаления и исправления данных."""

import json

from psycopg_pool import ConnectionPool

from app.config import get_settings
from app.errors import DomainError
from app.graph_repository import GraphRepository
from app.graph_store import GraphStore


def audit(pool, store):
    with pool.connection() as connection:
        versions = connection.execute(
            "SELECT program_id,id FROM program_versions ORDER BY program_id,id"
        ).fetchall()
    repo = GraphRepository(pool)
    issues = []
    for program_id, version_id in versions:
        try:
            store.read(repo.projection(program_id, version_id))
        except DomainError as error:
            issues.append(
                {
                    "program_id": str(program_id),
                    "version_id": str(version_id),
                    "status": "missing" if error.status == 409 else "inconsistent",
                }
            )
    with store.driver.session(database=store.database) as session:
        scopes = session.run("""MATCH (n) WHERE n:Entity OR n:ProgramVersionGraph
            RETURN DISTINCT n.program_id AS p,n.version_id AS v""")
        known = {(str(p), str(v)) for p, v in versions}
        orphans = [
            {"program_id": row["p"], "version_id": row["v"]}
            for row in scopes
            if (row["p"], row["v"]) not in known
        ]
    return {"versions_checked": len(versions), "issues": issues, "orphan_graphs": orphans}


def main():
    settings = get_settings()
    pool = ConnectionPool(
        kwargs={
            "host": settings.postgres_host,
            "dbname": settings.postgres_db,
            "user": settings.postgres_user,
            "password": settings.postgres_password.get_secret_value(),
        },
        min_size=0,
        max_size=2,
    )
    store = GraphStore(settings)
    try:
        result = audit(pool, store)
        print(json.dumps(result, ensure_ascii=False))
        return bool(result["issues"] or result["orphan_graphs"])
    finally:
        store.close()
        pool.close()


if __name__ == "__main__":
    raise SystemExit(main())
