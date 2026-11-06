"""Проверка предметных таблиц на настоящем PostgreSQL."""

import os
from uuid import UUID, uuid4

import psycopg
import pytest
from fastapi.testclient import TestClient

from app.domain import Entity, RelationV2, SnapshotV2
from app.main import app
from app.repository import Repository

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(os.getenv("RUN_INTEGRATION") != "1", reason="Нужен тестовый PostgreSQL"),
]


def test_save_restore_and_version_foreign_keys():
    with TestClient(app) as client:
        program = client.post("/api/v1/programs", json={"name": "Реляционные версии"}).json()
        repo = Repository(app.state.resources.postgres)
        pid = UUID(program["id"])
        a, b, relation = uuid4(), uuid4(), uuid4()
        snapshot = SnapshotV2(
            entities=[Entity(id=a, kind="discipline"), Entity(id=b, kind="competency")],
            relations=[RelationV2(id=relation, source_id=a, target_id=b, type="FORMS_COMPETENCY")],
        )
        version = repo.save(pid, UUID(program["current_version_id"]), uuid4().hex, snapshot)
        restored = repo.save(pid, version["id"], uuid4().hex, restore_id=version["id"])
        with repo.pool.connection() as conn:
            assert (
                conn.execute(
                    "SELECT count(*) FROM entities WHERE version_id=%s", (version["id"],)
                ).fetchone()[0]
                == 2
            )
            assert (
                conn.execute(
                    "SELECT count(*) FROM relations WHERE version_id=%s", (restored["id"],)
                ).fetchone()[0]
                == 1
            )
            with pytest.raises(psycopg.errors.ForeignKeyViolation):
                with conn.transaction():
                    conn.execute(
                        "INSERT INTO relations(version_id,id,source_entity_id,"
                        "target_entity_id,type)"
                        " VALUES(%s,%s,%s,%s,'HAS_TOPIC')",
                        (UUID(program["current_version_id"]), uuid4(), a, b),
                    )
            with pytest.raises(psycopg.errors.CheckViolation):
                with conn.transaction():
                    conn.execute(
                        "UPDATE entities SET kind='competency' WHERE version_id=%s",
                        (version["id"],),
                    )


def test_projection_failure_rolls_back_snapshot_and_head(monkeypatch):
    from app import relational_storage

    with TestClient(app) as client:
        program = client.post("/api/v1/programs", json={"name": "Откат проекции"}).json()
        repo = Repository(app.state.resources.postgres)
        pid, head = UUID(program["id"]), UUID(program["current_version_id"])

        def fail(*args):
            raise RuntimeError("Ошибка сохранения предметных записей")

        monkeypatch.setattr(relational_storage, "insert_projection", fail)
        with pytest.raises(RuntimeError, match="предметных записей"):
            repo.save(pid, head, uuid4().hex, SnapshotV2())
        assert repo.program(pid)["current_version_id"] == head
        assert len(repo.versions(pid)) == 1


def test_v2_field_evidence_matrix_and_restore_are_relational():
    from app.review_storage import insert_version

    with TestClient(app) as client:
        program = client.post("/api/v1/programs", json={"name": "Источники v2"}).json()
        root = "/api/v1/programs/" + program["id"]
        document = client.post(root + "/documents", files={"file": ("a.pdf", b"source")}).json()[
            "id"
        ]
        a, b, relation, ref, link, version_id = [uuid4() for _ in range(6)]
        snapshot = SnapshotV2.model_validate(
            {
                "document_ids": [document],
                "entities": [{"id": a, "kind": "discipline"}, {"id": b, "kind": "competency"}],
                "relations": [
                    {
                        "id": relation,
                        "source_id": a,
                        "target_id": b,
                        "type": "FORMS_COMPETENCY",
                        "attributes": {"matrix_value": 0},
                    }
                ],
                "source_refs": [{"id": ref, "document_id": document, "locator": "page:1"}],
                "source_links": [
                    {
                        "id": link,
                        "target_kind": "relation",
                        "target_id": relation,
                        "field_path": "/attributes/matrix_value",
                        "source_ref_id": ref,
                        "observed_value": {"value": 0},
                    }
                ],
            }
        )
        repo = Repository(app.state.resources.postgres)
        from psycopg.rows import dict_row

        with repo.pool.connection() as conn, conn.cursor(row_factory=dict_row) as cur:
            insert_version(
                cur, UUID(program["id"]), UUID(program["current_version_id"]), version_id, snapshot
            )
        restored = repo.save(UUID(program["id"]), version_id, uuid4().hex, restore_id=version_id)
        with repo.pool.connection() as conn:
            for vid in (version_id, restored["id"]):
                assert conn.execute(
                    "SELECT matrix_value FROM relations WHERE version_id=%s", (vid,)
                ).fetchone() == (0,)
                row = conn.execute(
                    "SELECT entity_id,relation_id,observed_value FROM source_links"
                    " WHERE version_id=%s",
                    (vid,),
                ).fetchone()
                assert row == (None, relation, {"value": 0})
            with pytest.raises(psycopg.errors.CheckViolation):
                with conn.transaction():
                    conn.execute(
                        "INSERT INTO source_links(id,version_id,entity_id,relation_id,"
                        "field_path,source_ref_id,observed_value) VALUES(%s,%s,%s,%s,'$',%s,'{}')",
                        (uuid4(), version_id, a, relation, ref),
                    )
        assert client.get(root + "/versions/" + str(version_id)).json()["snapshot"]["source_links"][
            0
        ]["id"] == str(link)
