"""Принятие последовательных предложений через HTTP и генерация итогового DOCX."""

import io
import os
import zipfile
from concurrent.futures import ThreadPoolExecutor
from uuid import uuid4

import httpx
import psycopg
import pytest
from psycopg.types.json import Jsonb
from test_review_integration import choose, make_docx, post, prepare, wait

from app.config import get_settings

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        os.getenv("REVIEW_FIXTURE_TESTS") != "1", reason="Нужен тестовый анализатор"
    ),
]


def saved_proposal(
    program,
    session,
    entity,
    refs,
    old,
    new,
    unresolved_questions=None,
    field_path="hours_total",
):
    """Сохраняет ответ агента без обращения к модели; дальше работает настоящий backend."""
    settings = get_settings()
    turn = uuid4()
    proposal = {
        "kind": "change_proposal",
        "summary": "Изменить часы",
        "changes": [
            {
                "target_kind": "entity",
                "target_id": entity,
                "field_path": field_path,
                "expected_old_value": old,
                "new_value": new,
                "reason": "Выбор пользователя",
                "source_ref_ids": refs,
            }
        ],
        "unresolved_questions": unresolved_questions or [],
    }
    with psycopg.connect(
        host=settings.postgres_host,
        dbname=settings.postgres_db,
        user=settings.postgres_user,
        password=settings.postgres_password.get_secret_value(),
    ) as conn:
        conn.execute(
            "INSERT INTO chat_turns(id,session_id,operation_key,request_hash,user_text,"
            "workflow_id,status,result,completed_at) "
            "VALUES(%s,%s,%s,%s,%s,%s,'completed',%s,now())",
            (
                turn,
                session,
                str(turn),
                "0" * 64,
                "Изменить часы",
                "chat-" + str(turn),
                Jsonb(proposal),
            ),
        )
    return str(turn)


def saved_multi_field_proposal(program, session, changes):
    """Сохраняет одно предложение с несколькими независимыми полями."""
    settings = get_settings()
    turn = uuid4()
    proposal = {
        "kind": "change_proposal",
        "summary": "Изменить несколько полей",
        "changes": changes,
        "unresolved_questions": [],
    }
    with psycopg.connect(
        host=settings.postgres_host,
        dbname=settings.postgres_db,
        user=settings.postgres_user,
        password=settings.postgres_password.get_secret_value(),
    ) as conn:
        conn.execute(
            "INSERT INTO chat_turns(id,session_id,operation_key,request_hash,user_text,"
            "workflow_id,status,result,completed_at) "
            "VALUES(%s,%s,%s,%s,%s,%s,'completed',%s,now())",
            (
                turn,
                session,
                str(turn),
                "0" * 64,
                "Изменить несколько полей",
                "chat-" + str(turn),
                Jsonb(proposal),
            ),
        )
    return str(turn)


@pytest.mark.parametrize("final", [180, 108])
def test_two_accepted_chat_proposals_check_and_docx(final):
    with httpx.Client(base_url="http://frontend", timeout=30, trust_env=False) as http:
        original = make_docx("<w:p><w:r><w:t>Тестовая дисциплина: 108 часов</w:t></w:r></w:p>")
        root, _, review = prepare(http, "РПД/РПД_матанализ1.docx", content=original)
        version, issue = choose(http, root, review)
        session = post(http, root + "/chat/sessions", {"version_id": version["id"]}, 201)
        program = root.split("/")[-1]
        entity = version["snapshot"]["entities"][0]
        for old, new in [(144, 160), (160, final)]:
            turn = saved_proposal(
                program, session["id"], entity["id"], entity["source_ref_ids"], old, new
            )
            path = root + f"/chat/sessions/{session['id']}/runs/{turn}/accept"
            body = {"expected_version_id": version["id"], "signature": "Тестовый пользователь"}
            key = uuid4().hex
            with ThreadPoolExecutor(max_workers=2) as executor:
                futures = [executor.submit(post, http, path, body, 201, key) for _ in range(2)]
                responses = [future.result() for future in futures]
            accepted = responses[0]
            assert responses[1] == accepted
            assert post(http, path, body, 201, key) == accepted
            rejected = http.post(path, json=body, headers={"Idempotency-Key": uuid4().hex})
            assert rejected.status_code == 409
            version = accepted["version"]
            assert version["snapshot"]["review"]["confirmation"] is None
            assert version["snapshot"]["entities"][0]["attributes"]["hours_total"] == new
            checked = wait(
                http, root + f"/reviews/{review['id']}/checks/" + accepted["check_job_id"]
            )
            assert checked["status"] == "completed", checked
            details = http.get(root + f"/versions/{version['id']}/review").json()
            assert details["check_report"]["coverage"][0]["reason"] == f"Получено часов: {new}"
        rejected = http.post(
            root + f"/reviews/{review['id']}/decisions",
            json={
                "expected_version_id": version["id"],
                "conflict_id": issue["id"],
                "variant_id": issue["variants"][0]["id"],
                "signature": "Тестовый пользователь",
            },
            headers={"Idempotency-Key": uuid4().hex},
        )
        if final == 108:
            assert rejected.status_code == 201, rejected.text
            version = rejected.json()
            assert version["snapshot"]["entities"][0]["attributes"]["hours_total"] == final
            assert len(version["snapshot"]["review"]["decisions"]) == 3
            details = http.get(root + f"/versions/{version['id']}/review").json()
            checked = wait(
                http, root + f"/reviews/{review['id']}/checks/"
                + details["lifecycle"]["check_job_id"],
            )
        else:
            assert rejected.status_code == 409, rejected.text
        assert http.get(root).json()["current_version_id"] == version["id"]
        assert checked["status"] == "completed"
        confirmed = version
        job = post(http, root + "/generation", {"version_id": confirmed["id"]}, 202)
        generated = wait(http, root + "/generation/" + job["id"])
        assert generated["status"] == "completed", generated
        content = http.get(root + "/generation/" + job["id"] + "/download").content
        with zipfile.ZipFile(io.BytesIO(content)) as archive:
            docx = next(archive.read(name) for name in archive.namelist() if name.endswith(".docx"))
        with zipfile.ZipFile(io.BytesIO(docx)) as archive:
            assert f"{final} часов" in archive.read("word/document.xml").decode()


def test_sequential_chat_changes_to_different_fields_are_both_generated():
    with httpx.Client(base_url="http://frontend", timeout=30, trust_env=False) as http:
        original = make_docx(
            "<w:p><w:r><w:t>Тестовая дисциплина: 108 часов, 3 ЗЕТ</w:t></w:r></w:p>"
        )
        root, _, review = prepare(http, "РПД/РПД_матанализ1.docx", content=original)
        version, _ = choose(http, root, review)
        session = post(http, root + "/chat/sessions", {"version_id": version["id"]}, 201)
        program = root.split("/")[-1]
        entity = version["snapshot"]["entities"][0]

        first_turn = saved_proposal(
            program, session["id"], entity["id"], entity["source_ref_ids"], 144, 160
        )
        first = post(
            http,
            root + f"/chat/sessions/{session['id']}/runs/{first_turn}/accept",
            {"expected_version_id": version["id"], "signature": "Тестовый пользователь"},
            201,
        )
        first_check = wait(http, root + f"/reviews/{review['id']}/checks/{first['check_job_id']}")
        assert first_check["status"] == "completed", first_check
        version = first["version"]
        entity = version["snapshot"]["entities"][0]

        second_turn = saved_proposal(
            program,
            session["id"],
            entity["id"],
            entity["source_ref_ids"],
            3,
            2,
            field_path="credit_units",
        )
        second = post(
            http,
            root + f"/chat/sessions/{session['id']}/runs/{second_turn}/accept",
            {"expected_version_id": version["id"], "signature": "Тестовый пользователь"},
            201,
        )
        checked = wait(http, root + f"/reviews/{review['id']}/checks/{second['check_job_id']}")
        assert checked["status"] == "completed", checked
        version = second["version"]

        assert checked["status"] == "completed"
        confirmed = version
        generation = post(http, root + "/generation", {"version_id": confirmed["id"]}, 202)
        generated = wait(http, root + "/generation/" + generation["id"])
        assert generated["status"] == "completed", generated
        package = http.get(root + "/generation/" + generation["id"] + "/download").content
        with zipfile.ZipFile(io.BytesIO(package)) as archive:
            docx = next(archive.read(name) for name in archive.namelist() if name.endswith(".docx"))
        with zipfile.ZipFile(io.BytesIO(docx)) as archive:
            xml = archive.read("word/document.xml").decode()
        assert "160 часов" in xml
        assert "2 ЗЕТ" in xml
        assert "108 часов" not in xml
        assert "3 ЗЕТ" not in xml


def test_each_change_in_one_chat_proposal_gets_its_own_check():
    with httpx.Client(base_url="http://frontend", timeout=30, trust_env=False) as http:
        original = make_docx("<w:p><w:r><w:t>Тестовая дисциплина: 108 часов</w:t></w:r></w:p>")
        root, _, review = prepare(http, "РПД/РПД_матанализ1.docx", content=original)
        version, _ = choose(http, root, review)
        session = post(http, root + "/chat/sessions", {"version_id": version["id"]}, 201)
        entity = next(
            item for item in version["snapshot"]["entities"] if item["kind"] == "discipline"
        )
        links = [
            item
            for item in version["snapshot"]["source_links"]
            if item["target_kind"] == "entity" and item["target_id"] == entity["id"]
        ]
        fields = {}
        for link in links:
            parts = link["field_path"].split("/")
            if len(parts) == 3 and parts[1] == "attributes":
                name = parts[2]
                old = entity["attributes"].get(name)
                if isinstance(old, str):
                    fields.setdefault(name, (old, link["source_ref_id"]))
        assert "hours_total" in entity["attributes"]
        assert fields
        alternate_field = next(iter(fields))
        alternate_old, alternate_ref = fields[alternate_field]
        hours_link = next(item for item in links if item["field_path"] == "/attributes/hours_total")
        changes = [
            {
                "target_kind": "entity",
                "target_id": entity["id"],
                "field_path": "/attributes/hours_total",
                "expected_old_value": entity["attributes"]["hours_total"],
                "new_value": entity["attributes"]["hours_total"] + 12,
                "reason": "Проверка первого шага",
                "source_ref_ids": [hours_link["source_ref_id"]],
            },
            {
                "target_kind": "entity",
                "target_id": entity["id"],
                "field_path": f"/attributes/{alternate_field}",
                "expected_old_value": alternate_old,
                "new_value": alternate_old + " исправлено",
                "reason": "Проверка второго шага",
                "source_ref_ids": [alternate_ref],
            },
        ]
        program = root.split("/")[-1]
        turn = saved_multi_field_proposal(program, session["id"], changes)
        accepted = post(
            http,
            root + f"/chat/sessions/{session['id']}/runs/{turn}/accept",
            {"expected_version_id": version["id"], "signature": "Тестовый пользователь"},
            201,
        )
        settings = get_settings()
        with psycopg.connect(
            host=settings.postgres_host,
            dbname=settings.postgres_db,
            user=settings.postgres_user,
            password=settings.postgres_password.get_secret_value(),
        ) as conn:
            rows = conn.execute(
                "WITH RECURSIVE ancestry(id,parent_version_id,depth) AS ("
                "SELECT id,parent_version_id,0 FROM program_versions WHERE id=%s "
                "UNION ALL SELECT parent.id,parent.parent_version_id,ancestry.depth+1 "
                "FROM program_versions parent "
                "JOIN ancestry ON parent.id=ancestry.parent_version_id "
                "WHERE ancestry.depth<2) "
                "SELECT j.id,j.input_version_id,ancestry.depth FROM processing_jobs j "
                "JOIN ancestry ON ancestry.id=j.input_version_id "
                "WHERE j.type='validate' AND j.parameters->>'purpose'='check' "
                "AND ancestry.depth<2 ORDER BY ancestry.depth DESC",
                (accepted["version"]["id"],),
            ).fetchall()
        assert len(rows) == 2
        for job_id, _, _ in rows:
            checked = wait(http, root + f"/reviews/{review['id']}/checks/{job_id}")
            assert checked["status"] == "completed", checked


def test_invalid_second_chat_change_rolls_back_versions_jobs_and_session():
    with httpx.Client(base_url="http://frontend", timeout=30, trust_env=False) as http:
        original = make_docx("<w:p><w:r><w:t>Тестовая дисциплина: 108 часов</w:t></w:r></w:p>")
        root, _, review = prepare(http, "РПД/РПД_матанализ1.docx", content=original)
        version, _ = choose(http, root, review)
        session = post(http, root + "/chat/sessions", {"version_id": version["id"]}, 201)
        entity = next(
            item for item in version["snapshot"]["entities"] if item["kind"] == "discipline"
        )
        links = [
            item
            for item in version["snapshot"]["source_links"]
            if item["target_kind"] == "entity" and item["target_id"] == entity["id"]
        ]
        hours_link = next(item for item in links if item["field_path"] == "/attributes/hours_total")
        name_link = next(item for item in links if item["field_path"] == "/attributes/name")
        name = entity["attributes"]["name"]
        assert isinstance(name, str)
        turn = saved_multi_field_proposal(
            root.rsplit("/", 1)[-1],
            session["id"],
            [
                {
                    "target_kind": "entity",
                    "target_id": entity["id"],
                    "field_path": "/attributes/hours_total",
                    "expected_old_value": entity["attributes"]["hours_total"],
                    "new_value": entity["attributes"]["hours_total"] + 12,
                    "reason": "Допустимая первая правка",
                    "source_ref_ids": [hours_link["source_ref_id"]],
                },
                {
                    "target_kind": "entity",
                    "target_id": entity["id"],
                    "field_path": "/attributes/name",
                    "expected_old_value": name,
                    "new_value": 123,
                    "reason": "Невалидная формулировка",
                    "source_ref_ids": [name_link["source_ref_id"]],
                },
            ],
        )
        settings = get_settings()
        program_id = root.rsplit("/", 1)[-1]
        with psycopg.connect(
            host=settings.postgres_host,
            dbname=settings.postgres_db,
            user=settings.postgres_user,
            password=settings.postgres_password.get_secret_value(),
        ) as conn:
            before = conn.execute(
                "SELECT (SELECT count(*) FROM program_versions WHERE program_id=%s), "
                "       (SELECT count(*) FROM processing_jobs WHERE program_id=%s "
                "        AND type='validate' AND parameters->>'purpose'='check'), "
                "       (SELECT version_id FROM chat_sessions WHERE id=%s)",
                (program_id, program_id, session["id"]),
            ).fetchone()

        response = http.post(
            root + f"/chat/sessions/{session['id']}/runs/{turn}/accept",
            json={"expected_version_id": version["id"], "signature": "Тестовый пользователь"},
            headers={"Idempotency-Key": uuid4().hex},
        )
        assert response.status_code == 422, response.text

        with psycopg.connect(
            host=settings.postgres_host,
            dbname=settings.postgres_db,
            user=settings.postgres_user,
            password=settings.postgres_password.get_secret_value(),
        ) as conn:
            after = conn.execute(
                "SELECT (SELECT count(*) FROM program_versions WHERE program_id=%s), "
                "       (SELECT count(*) FROM processing_jobs WHERE program_id=%s "
                "        AND type='validate' AND parameters->>'purpose'='check'), "
                "       (SELECT version_id FROM chat_sessions WHERE id=%s)",
                (program_id, program_id, session["id"]),
            ).fetchone()
        assert after == before


def test_chat_accept_rejects_stale_version_value_and_unresolved_question():
    with httpx.Client(base_url="http://frontend", timeout=30, trust_env=False) as http:
        original = make_docx("<w:p><w:r><w:t>Тестовая дисциплина: 108 часов</w:t></w:r></w:p>")
        root, _, review = prepare(http, "РПД/РПД_матанализ1.docx", content=original)
        base_version, _ = choose(http, root, review)
        program_id = root.rsplit("/", 1)[-1]
        entity = next(
            item for item in base_version["snapshot"]["entities"] if item["kind"] == "discipline"
        )
        refs = entity["source_ref_ids"]
        working_session = post(
            http, root + "/chat/sessions", {"version_id": base_version["id"]}, 201
        )
        stale_session = post(http, root + "/chat/sessions", {"version_id": base_version["id"]}, 201)
        accepted_turn = saved_proposal(
            program_id, working_session["id"], entity["id"], refs, 144, 160
        )
        accepted = post(
            http,
            root + f"/chat/sessions/{working_session['id']}/runs/{accepted_turn}/accept",
            {"expected_version_id": base_version["id"], "signature": "Тестовый пользователь"},
            201,
        )
        checked = wait(http, root + f"/reviews/{review['id']}/checks/{accepted['check_job_id']}")
        assert checked["status"] == "completed", checked
        current_version = accepted["version"]
        current_entity = next(
            item for item in current_version["snapshot"]["entities"] if item["kind"] == "discipline"
        )
        current_refs = current_entity["source_ref_ids"]
        unresolved_session = post(
            http, root + "/chat/sessions", {"version_id": current_version["id"]}, 201
        )
        stale_value_session = post(
            http, root + "/chat/sessions", {"version_id": current_version["id"]}, 201
        )
        stale_turn = saved_proposal(program_id, stale_session["id"], entity["id"], refs, 144, 180)
        unresolved_turn = saved_proposal(
            program_id,
            unresolved_session["id"],
            current_entity["id"],
            current_refs,
            160,
            180,
            unresolved_questions=["Уточните семестр"],
        )
        stale_value_turn = saved_proposal(
            program_id, stale_value_session["id"], current_entity["id"], current_refs, 144, 180
        )
        settings = get_settings()
        with psycopg.connect(
            host=settings.postgres_host,
            dbname=settings.postgres_db,
            user=settings.postgres_user,
            password=settings.postgres_password.get_secret_value(),
        ) as conn:
            before = conn.execute(
                "SELECT (SELECT count(*) FROM program_versions WHERE program_id=%s), "
                "(SELECT count(*) FROM processing_jobs WHERE program_id=%s "
                "AND type='validate' AND parameters->>'purpose'='check')",
                (program_id, program_id),
            ).fetchone()

        def accept_status(session, turn, expected_version):
            return http.post(
                root + f"/chat/sessions/{session['id']}/runs/{turn}/accept",
                json={
                    "expected_version_id": expected_version,
                    "signature": "Тестовый пользователь",
                },
                headers={"Idempotency-Key": uuid4().hex},
            )

        assert accept_status(stale_session, stale_turn, base_version["id"]).status_code == 409
        unresolved = accept_status(unresolved_session, unresolved_turn, current_version["id"])
        assert unresolved.status_code == 409, unresolved.text
        stale_value = accept_status(stale_value_session, stale_value_turn, current_version["id"])
        assert stale_value.status_code == 422, stale_value.text
        assert http.get(root).json()["current_version_id"] == current_version["id"]
        assert (
            http.get(root + f"/chat/sessions/{unresolved_session['id']}").json()["version_id"]
            == current_version["id"]
        )
        with psycopg.connect(
            host=settings.postgres_host,
            dbname=settings.postgres_db,
            user=settings.postgres_user,
            password=settings.postgres_password.get_secret_value(),
        ) as conn:
            after = conn.execute(
                "SELECT (SELECT count(*) FROM program_versions WHERE program_id=%s), "
                "(SELECT count(*) FROM processing_jobs WHERE program_id=%s "
                "AND type='validate' AND parameters->>'purpose'='check')",
                (program_id, program_id),
            ).fetchone()
        assert after == before
