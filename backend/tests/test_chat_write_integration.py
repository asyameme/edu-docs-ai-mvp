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
from test_generation_docx_patch import make_docx
from test_review_integration import choose, post, prepare, wait

from app.config import get_settings

pytestmark = [pytest.mark.integration, pytest.mark.skipif(
    os.getenv("REVIEW_FIXTURE_TESTS") != "1", reason="Нужен тестовый анализатор"
)]


def saved_proposal(program, session, entity, refs, old, new):
    """Сохраняет ответ агента без обращения к модели; дальше работает настоящий backend."""
    settings = get_settings()
    turn = uuid4()
    proposal = {
        "kind": "change_proposal", "summary": "Изменить часы", "changes": [{
            "target_kind": "entity", "target_id": entity, "field_path": "hours_total",
            "expected_old_value": old, "new_value": new, "reason": "Выбор пользователя",
            "source_ref_ids": refs,
        }], "unresolved_questions": [],
    }
    with psycopg.connect(
        host=settings.postgres_host, dbname=settings.postgres_db,
        user=settings.postgres_user, password=settings.postgres_password.get_secret_value(),
    ) as conn:
        conn.execute(
            "INSERT INTO chat_turns(id,session_id,operation_key,request_hash,user_text,"
            "workflow_id,status,result,completed_at) "
            "VALUES(%s,%s,%s,%s,%s,%s,'completed',%s,now())",
            (turn, session, str(turn), "0" * 64, "Изменить часы", "chat-" + str(turn),
             Jsonb(proposal)),
        )
    return str(turn)


@pytest.mark.parametrize("final", [180, 108])
def test_two_accepted_chat_proposals_check_confirm_and_docx(final):
    with httpx.Client(base_url="http://frontend", timeout=30, trust_env=False) as http:
        original = make_docx('<w:p><w:r><w:t>Тестовая дисциплина: 108 часов</w:t></w:r></w:p>')
        root, _, review = prepare(http, "РПД/РПД_матанализ1.docx", content=original)
        version, _ = choose(http, root, review)
        session = post(http, root + "/chat/sessions", {"version_id": version["id"]}, 201)
        program = root.split("/")[-1]
        entity = version["snapshot"]["entities"][0]
        for old, new in [(144, 160), (160, final)]:
            turn = saved_proposal(program, session["id"], entity["id"],
                                  entity["source_ref_ids"], old, new)
            path = root + f'/chat/sessions/{session["id"]}/runs/{turn}/accept'
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
            checked = wait(http, root + f'/reviews/{review["id"]}/checks/'
                           + accepted["check_job_id"])
            assert checked["status"] == "completed", checked
            details = http.get(root + f'/versions/{version["id"]}/review').json()
            assert details["check_report"]["coverage"][0]["reason"] == f"Получено часов: {new}"
        confirmed = post(http, root + f'/reviews/{review["id"]}/confirm', {
            "expected_version_id": version["id"], "signature": "Тестовый пользователь",
            "check_job_id": accepted["check_job_id"],
        }, 201)
        job = post(http, root + "/generation", {"version_id": confirmed["id"]}, 202)
        generated = wait(http, root + "/generation/" + job["id"])
        assert generated["status"] == "completed", generated
        content = http.get(root + "/generation/" + job["id"] + "/download").content
        with zipfile.ZipFile(io.BytesIO(content)) as archive:
            docx = next(archive.read(name) for name in archive.namelist() if name.endswith(".docx"))
        with zipfile.ZipFile(io.BytesIO(docx)) as archive:
            assert f"{final} часов" in archive.read("word/document.xml").decode()
