"""Версии читаются из графа; история и независимые редакции проверяются в PostgreSQL."""

import os
from uuid import uuid4

import psycopg
import pytest
from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool

from app.domain import Entity, RelationV2, SnapshotV2
from app.repository import Repository

pytestmark = pytest.mark.skipif(
    os.getenv("GRAPH_TEST_DSN") is None, reason="Нужна отдельная PostgreSQL для графа"
)


@pytest.mark.parametrize("confidence", [0.0, 0.75, 1.0])
def test_graph_versions_reuse_unchanged_revisions_and_restore(confidence):
    with ConnectionPool(os.environ["GRAPH_TEST_DSN"]) as pool:
        repo = Repository(pool)
        program = repo.create_program("Переиспользование редакций")
        a, b = uuid4(), uuid4()
        snapshot = SnapshotV2(
            metadata={"label": "Проверка"},
            entities=[
                Entity(id=a, kind="discipline", attributes={"hours": 72}),
                Entity(id=b, kind="competency", attributes={"code": "УК-1"}),
            ],
            relations=[
                RelationV2(
                    id=uuid4(),
                    source_id=a,
                    target_id=b,
                    type="FORMS_COMPETENCY",
                    confidence=confidence,
                    attributes={"matrix_value": 0},
                )
            ],
        )
        first = repo.save(program["id"], program["current_version_id"], uuid4().hex, snapshot)
        assert first["snapshot"] == snapshot.model_dump(mode="json")
        changed = snapshot.model_copy(deep=True)
        changed.entities[0].attributes["hours"] = 108
        second = repo.save(program["id"], first["id"], uuid4().hex, changed)
        restored = repo.save(program["id"], second["id"], uuid4().hex, restore_id=first["id"])
        assert restored["snapshot"] == first["snapshot"]
        assert (
            repo.version(program["id"], first["id"])["snapshot"]["entities"][0]["attributes"][
                "hours"
            ]
            == 72
        )
        with pool.connection() as conn, conn.cursor(row_factory=dict_row) as cur:
            assert not cur.execute(
                "SELECT 1 FROM information_schema.columns WHERE "
                "table_schema='public' AND table_name='program_versions' "
                "AND column_name='snapshot'"
            ).fetchone()
            assert (
                cur.execute(
                    "SELECT count(*) AS n FROM entity_revisions WHERE program_id=%s",
                    (program["id"],),
                ).fetchone()["n"]
                == 3
            )
            states = cur.execute(
                "SELECT graph_state_id FROM program_versions WHERE id IN (%s,%s)",
                (first["id"], restored["id"]),
            ).fetchall()
            assert states[0] == states[1]
            assert cur.execute(
                "SELECT 1 FROM change_log WHERE version_id=%s AND "
                "field_path='/attributes/hours' AND old_value='72' "
                "AND new_value='108'",
                (second["id"],),
            ).fetchone()
            with pytest.raises(psycopg.errors.CheckViolation), conn.transaction():
                cur.execute(
                    "UPDATE entity_revisions SET attributes='{}' WHERE program_id=%s",
                    (program["id"],),
                )
def test_equivalent_json_numbers_do_not_break_version_journal():
    with ConnectionPool(os.environ["GRAPH_TEST_DSN"]) as pool:
        repo = Repository(pool)
        program = repo.create_program("Эквивалентные числа")
        snapshot = SnapshotV2(
            entities=[Entity(id=uuid4(), kind="discipline", attributes={"hours": 72})]
        )
        first = repo.save(
            program["id"], program["current_version_id"], uuid4().hex, snapshot
        )
        snapshot.entities[0].attributes["hours"] = 72.0
        second = repo.save(program["id"], first["id"], uuid4().hex, snapshot)
        with pool.connection() as conn:
            assert conn.execute(
                "SELECT count(*) FROM change_log WHERE version_id=%s", (second["id"],)
            ).fetchone()[0] == 0


def test_entity_order_change_is_written_to_journal():
    with ConnectionPool(os.environ["GRAPH_TEST_DSN"]) as pool:
        repo = Repository(pool)
        program = repo.create_program("Порядок узлов")
        snapshot = SnapshotV2(
            entities=[
                Entity(id=uuid4(), kind="discipline", attributes={"name": name})
                for name in ("Первый", "Второй")
            ]
        )
        first = repo.save(
            program["id"], program["current_version_id"], uuid4().hex, snapshot
        )
        snapshot.entities.reverse()
        second = repo.save(program["id"], first["id"], uuid4().hex, snapshot)
        with pool.connection() as conn:
            change = conn.execute(
                "SELECT old_value,new_value FROM change_log WHERE version_id=%s "
                "AND field_path='/entities/order'",
                (second["id"],),
            ).fetchone()
        assert change == (
            [str(item.id) for item in reversed(snapshot.entities)],
            [str(item.id) for item in snapshot.entities],
        )


def test_real_parser_artifacts_and_pinned_generation_after_import_retry():
    import hashlib
    import io
    from pathlib import Path
    from zipfile import ZipFile

    from starlette.datastructures import UploadFile

    from app.config import Settings
    from app.generation_activities import GenerationActivities
    from app.generation_repository import GenerationRepository
    from app.graph_storage import artifact_output
    from app.import_activities import ImportActivities
    from app.import_repository import ImportRepository
    from app.storage import OriginalStorage

    content = (Path(__file__).parents[2] / "tests/originals/РПД_матанализ1.docx").read_bytes()
    with ConnectionPool(os.environ["GRAPH_TEST_DSN"]) as pool:
        repo = Repository(pool)
        program = repo.create_program("Настоящий парсер и генератор")
        stored = OriginalStorage(10 * 1024 * 1024).save(
            UploadFile(file=io.BytesIO(content), filename="РПД_матанализ1.docx")
        )
        imports = ImportRepository(pool)
        job, _ = imports.create(
            program["id"], uuid4().hex, uuid4().hex, [("РПД/РПД_матанализ1.docx", stored, True)]
        )
        status = imports.job(job)
        document = status["files"][0]["document_id"]
        activities = ImportActivities(pool, Settings(postgres_password="test"))
        request = {"job_id": str(job), "document_id": str(document), "attempt": 1}
        activities.parse(request)
        activities.finish(request)
        parsed = imports.job(job, include_output=True)
        assert parsed["status"] in {"completed", "partial"}
        assert parsed["snapshot"]["entities"]
        version = repo.save(
            program["id"],
            program["current_version_id"],
            uuid4().hex,
            SnapshotV2.model_validate(parsed["snapshot"]),
        )
        assert version["snapshot"] == parsed["snapshot"]
        generation = GenerationRepository(pool)
        generated = generation.create(program["id"], None, job, uuid4().hex)
        before = generation.input(generated)[0].model_dump(mode="json")
        imports.retry(program["id"], job, parsed["_workflow_id"])
        # Повтор создаёт новый результат импорта, уже принятая генерация читает прежний граф.
        second = {**request, "attempt": 2}
        activities.finish(second)
        assert generation.input(generated)[0].model_dump(mode="json") == before
        assert GenerationActivities(pool).build({"job_id": str(generated)}) == "completed"
        package = generation.package(program["id"], generated)
        with ZipFile(io.BytesIO(bytes(package["content"]))) as archive:
            names = [name for name in archive.namelist() if name.endswith(".docx")]
            assert len(names) == 1
            assert (
                hashlib.sha256(archive.read(names[0])).digest() == hashlib.sha256(content).digest()
            )
        with pool.connection() as conn, conn.cursor(row_factory=dict_row) as cur:
            row = cur.execute(
                "SELECT report,parameters FROM processing_jobs WHERE id=%s", (job,)
            ).fetchone()
            assert "snapshot" not in row["report"]
            assert "output" not in row["report"]["files"][str(document)]
            assert (
                artifact_output(cur, row["report"]["files"][str(document)]["artifact_id"])
                == parsed["files"][0]["output"]
            )
            assert (
                "parsed_documents"
                not in cur.execute(
                    "SELECT metadata FROM graph_states WHERE id="
                    "(SELECT graph_state_id FROM program_versions WHERE id=%s)",
                    (version["id"],),
                ).fetchone()["metadata"]
            )
            assert (
                "snapshot"
                not in cur.execute(
                    "SELECT parameters FROM processing_jobs WHERE id=%s", (generated,)
                ).fetchone()["parameters"]
            )
            assert (
                "started_at"
                not in cur.execute(
                    "SELECT report FROM processing_jobs WHERE id=%s", (generated,)
                ).fetchone()["report"]
            )


def test_same_public_evidence_id_preserves_distinct_observations_and_json_types():
    import hashlib

    with ConnectionPool(os.environ["GRAPH_TEST_DSN"]) as pool:
        repo = Repository(pool)
        program = repo.create_program("Типы значений и наблюдения")
        document, entity, ref, link = [uuid4() for _ in range(4)]
        repo.add_document(
            program["id"],
            {
                "id": document,
                "original_name": "a.docx",
                "format": "docx",
                "media_type": "application/octet-stream",
                "size_bytes": 1,
                "sha256": hashlib.sha256(b"x").hexdigest(),
                "content": b"x",
            },
        )
        first = SnapshotV2.model_validate(
            {
                "document_ids": [str(document)],
                "entities": [
                    {
                        "id": str(entity),
                        "kind": "discipline",
                        "attributes": {"selection": 0},
                        "source_ref_ids": [str(ref)],
                    }
                ],
                "source_refs": [
                    {"id": str(ref), "document_id": str(document), "locator": "page:1"}
                ],
                "source_links": [
                    {
                        "id": str(link),
                        "target_kind": "entity",
                        "target_id": str(entity),
                        "field_path": "/attributes/selection",
                        "source_ref_id": str(ref),
                        "observed_value": {"value": 0},
                    }
                ],
            }
        )
        old = repo.save(program["id"], program["current_version_id"], uuid4().hex, first)
        changed = first.model_copy(deep=True)
        changed.entities[0].attributes["selection"] = False
        changed.source_links[0].observed_value["value"] = False
        new = repo.save(program["id"], old["id"], uuid4().hex, changed)
        assert new["snapshot"]["source_links"][0]["id"] == old["snapshot"]["source_links"][0]["id"]
        assert new["snapshot"]["source_links"][0]["observed_value"]["value"] is False
        assert (
            type(
                repo.version(program["id"], old["id"])["snapshot"]["source_links"][0][
                    "observed_value"
                ]["value"]
            )
            is int
        )
        with pool.connection() as conn:
            assert (
                conn.execute(
                    "SELECT count(*) FROM source_links WHERE program_id=%s", (program["id"],)
                ).fetchone()[0]
                == 2
            )
            assert conn.execute(
                "SELECT 1 FROM change_log WHERE version_id=%s AND "
                "field_path='/attributes/selection' "
                "AND old_value='0'::jsonb AND new_value='false'::jsonb",
                (new["id"],),
            ).fetchone()


def test_concurrent_saves_keep_one_current_head():
    from concurrent.futures import ThreadPoolExecutor

    from app.errors import DomainError

    with ConnectionPool(os.environ["GRAPH_TEST_DSN"]) as pool:
        repo = Repository(pool)
        program = repo.create_program("Конкурентное сохранение")

        def save(label):
            try:
                repo.save(
                    program["id"],
                    program["current_version_id"],
                    uuid4().hex,
                    SnapshotV2(metadata={"label": label}),
                )
                return 201
            except DomainError as error:
                return error.status

        with ThreadPoolExecutor(max_workers=2) as executor:
            assert sorted(executor.map(save, ["А", "Б"])) == [201, 409]
        assert len(repo.versions(program["id"])) == 2


def test_non_owner_cannot_disable_history_protection():
    from psycopg import sql

    with ConnectionPool(os.environ["GRAPH_TEST_DSN"]) as pool:
        repo = Repository(pool)
        program = repo.create_program("Права записи истории")
        role = "graph_test_" + uuid4().hex
        with pool.connection() as conn, conn.transaction(force_rollback=True):
            conn.execute(sql.SQL("CREATE ROLE {} NOLOGIN").format(sql.Identifier(role)))
            conn.execute(sql.SQL("GRANT USAGE ON SCHEMA public TO {}").format(sql.Identifier(role)))
            conn.execute(
                sql.SQL(
                    "GRANT SELECT,INSERT,UPDATE,DELETE ON ALL TABLES IN SCHEMA public TO {}"
                ).format(sql.Identifier(role))
            )
            conn.execute(sql.SQL("SET LOCAL ROLE {}").format(sql.Identifier(role)))
            with pytest.raises(psycopg.errors.InsufficientPrivilege), conn.transaction():
                conn.execute("ALTER TABLE program_versions DISABLE TRIGGER ALL")
            with pytest.raises(psycopg.errors.CheckViolation), conn.transaction():
                conn.execute(
                    "UPDATE program_versions SET review='{}' WHERE id=%s",
                    (program["current_version_id"],),
                )
            with pytest.raises(psycopg.errors.InsufficientPrivilege), conn.transaction():
                conn.execute("TRUNCATE change_log")


def test_temporary_table_cannot_bypass_sealed_graph_membership_guard():
    from psycopg import sql

    with ConnectionPool(os.environ["GRAPH_TEST_DSN"]) as pool:
        repo = Repository(pool)
        program = repo.create_program("Защита запечатанного графа")
        snapshot = SnapshotV2(
            entities=[Entity(id=uuid4(), kind="discipline", attributes={"hours": 72})]
        )
        version = repo.save(
            program["id"], program["current_version_id"], uuid4().hex, snapshot
        )
        with pool.connection() as conn:
            state_id = conn.execute(
                "SELECT graph_state_id FROM program_versions WHERE id=%s", (version["id"],)
            ).fetchone()[0]
        role = "graph_temp_" + uuid4().hex
        with pool.connection() as conn, conn.transaction(force_rollback=True):
            conn.execute(sql.SQL("CREATE ROLE {} NOLOGIN").format(sql.Identifier(role)))
            conn.execute(sql.SQL("GRANT USAGE ON SCHEMA public TO {}").format(sql.Identifier(role)))
            conn.execute(
                sql.SQL("GRANT SELECT ON programs,graph_states,graph_entities TO {}").format(
                    sql.Identifier(role)
                )
            )
            conn.execute(
                sql.SQL("GRANT UPDATE ON programs,graph_entities TO {}").format(
                    sql.Identifier(role)
                )
            )
            conn.execute(sql.SQL("SET LOCAL ROLE {}").format(sql.Identifier(role)))
            conn.execute(
                "CREATE TEMP TABLE graph_states AS SELECT id,program_id,'building'::text status "
                "FROM public.graph_states"
            )
            with pytest.raises(psycopg.errors.CheckViolation):
                with conn.transaction():
                    conn.execute(
                        "UPDATE public.graph_entities SET position=position+1 "
                        "WHERE graph_state_id=%s",
                        (state_id,),
                    )


def test_restore_legacy_state_reuses_graph_and_returns_valid_current_dto():
    from app.domain import SnapshotData, VersionRead
    from app.graph_storage import save_graph

    with ConnectionPool(os.environ["GRAPH_TEST_DSN"]) as pool:
        repo = Repository(pool)
        program = repo.create_program("Восстановление V1")
        version = uuid4()
        legacy = {
            "schema_version": 1,
            **SnapshotData(metadata={"legacy": True}).model_dump(mode="json"),
        }
        with pool.connection() as conn, conn.cursor(row_factory=dict_row) as cur:
            state = save_graph(cur, program["id"], legacy)
            cur.execute(
                "INSERT INTO program_versions(id,program_id,number,"
                "parent_version_id,graph_state_id,origin) "
                "VALUES(%s,%s,2,%s,%s,'save')",
                (version, program["id"], program["current_version_id"], state),
            )
            cur.execute(
                "UPDATE programs SET current_version_id=%s WHERE id=%s", (version, program["id"])
            )
        restored = repo.save(program["id"], version, uuid4().hex, restore_id=version)
        assert VersionRead.model_validate(restored).snapshot.metadata == {"legacy": True}
        with pool.connection() as conn:
            assert (
                conn.execute(
                    "SELECT graph_state_id FROM program_versions WHERE id=%s", (restored["id"],)
                ).fetchone()[0]
                == state
            )
            assert (
                conn.execute(
                    "SELECT schema_version FROM graph_states WHERE id=%s", (state,)
                ).fetchone()[0]
                == 1
            )
