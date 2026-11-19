"""Сквозная проверка HTTP, Temporal и ML-агента на локальной тестовой модели."""

import json
import os
import re
import time
from concurrent.futures import ThreadPoolExecutor
from uuid import UUID, uuid4

import httpx
import psycopg
import pytest
from test_generation_docx_patch import _paragraph, make_docx
from test_review_integration import choose, post, prepare, wait

from app.config import get_settings

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        os.getenv("CHAT_AGENT_FIXTURE_TESTS") != "1",
        reason="Требуются отдельные test model, HTTP, PostgreSQL и Temporal сервисы",
    ),
]


def send_chat_message(http, root, session_id, text):
    response = http.post(
        root + f"/chat/sessions/{session_id}/messages",
        json={"text": text},
        headers={"Idempotency-Key": uuid4().hex},
        timeout=90,
    )
    assert response.status_code == 202, response.text
    run_id = response.json()["run_id"]
    events = http.get(root + f"/chat/runs/{run_id}/events", timeout=90)
    assert events.status_code == 200, events.text
    assert "event: completed" in events.text, events.text
    data = [line[6:] for line in events.text.splitlines() if line.startswith("data: ")]
    return run_id, json.loads(data[-1])


def read_terminal_event(http, url, timeout_seconds=60):
    deadline = time.monotonic() + timeout_seconds
    lines = []
    terminal_event = False
    with http.stream("GET", url, timeout=90) as response:
        assert response.status_code == 200
        for line in response.iter_lines():
            lines.append(line)
            if line in {"event: completed", "event: failed"}:
                terminal_event = True
            elif terminal_event and line.startswith("data: "):
                break
            if time.monotonic() >= deadline:
                pytest.fail("Ход чата не завершился за отведённое время")
    return "\n".join(lines)


def test_chat_agent_model_failure_is_hidden_from_public_sse():
    with httpx.Client(base_url="http://frontend", timeout=30, trust_env=False) as http:
        root, _, review = prepare(
            http,
            "РПД/РПД_матанализ1.docx",
            content=make_docx(_paragraph("Тестовая дисциплина: 108 часов")),
        )
        version, _ = choose(http, root, review)
        session = post(
            http,
            root + "/chat/sessions",
            {"version_id": version["id"]},
            201,
        )
        response = http.post(
            root + f"/chat/sessions/{session['id']}/messages",
            json={"text": "fixture-model-failure"},
            headers={"Idempotency-Key": uuid4().hex},
            timeout=30,
        )
        assert response.status_code == 202, response.text
        run_id = response.json()["run_id"]

        events = http.get(root + f"/chat/runs/{run_id}/events", timeout=90)
        assert events.status_code == 200, events.text
        assert "event: failed" in events.text, events.text
        assert "Не удалось выполнить запрос" in events.text
        assert "Synthetic chat model failure" not in events.text
        assert "Traceback" not in events.text

        history = http.get(root + f"/chat/sessions/{session['id']}").json()
        assert history["turns"][-1]["status"] == "failed"
        assert history["turns"][-1]["result"] is None
        assert "conversation" not in history["turns"][-1]


def test_chat_agent_model_timeout_is_hidden_from_public_sse():
    with httpx.Client(base_url="http://frontend", timeout=30, trust_env=False) as http:
        root, _, review = prepare(
            http,
            "РПД/РПД_матанализ1.docx",
            content=make_docx(_paragraph("Тестовая дисциплина: 108 часов")),
        )
        version, _ = choose(http, root, review)
        session = post(
            http,
            root + "/chat/sessions",
            {"version_id": version["id"]},
            201,
        )
        response = http.post(
            root + f"/chat/sessions/{session['id']}/messages",
            json={"text": "fixture-model-timeout"},
            headers={"Idempotency-Key": uuid4().hex},
            timeout=30,
        )
        assert response.status_code == 202, response.text
        run_id = response.json()["run_id"]

        events = read_terminal_event(http, root + f"/chat/runs/{run_id}/events")
        assert "event: failed" in events, events
        assert "Не удалось выполнить запрос" in events
        assert "timeout" not in events.lower()
        assert "Traceback" not in events

        history = http.get(root + f"/chat/sessions/{session['id']}").json()
        assert history["turns"][-1]["status"] == "failed"
        assert history["turns"][-1]["result"] is None
        assert "conversation" not in history["turns"][-1]


def test_chat_message_idempotency_and_parallel_messages():
    with httpx.Client(base_url="http://frontend", timeout=30, trust_env=False) as http:
        root, _, review = prepare(
            http,
            "РПД/РПД_матанализ1.docx",
            content=make_docx(_paragraph("Тестовая дисциплина: 108 часов")),
        )
        version, _ = choose(http, root, review)
        session = post(
            http,
            root + "/chat/sessions",
            {"version_id": version["id"]},
            201,
        )
        text = "fixture-slow: Сколько часов в Тестовая дисциплина?"
        keys = [uuid4().hex, uuid4().hex]

        def send(key, message):
            return http.post(
                root + f"/chat/sessions/{session['id']}/messages",
                json={"text": message},
                headers={"Idempotency-Key": key},
                timeout=30,
            )

        with ThreadPoolExecutor(max_workers=2) as executor:
            responses = list(executor.map(lambda key: send(key, text), keys))
        accepted = [response for response in responses if response.status_code == 202]
        rejected = [response for response in responses if response.status_code == 409]
        assert len(accepted) == len(rejected) == 1, [
            (response.status_code, response.text) for response in responses
        ]

        turn = accepted[0].json()
        accepted_key = keys[responses.index(accepted[0])]
        replay = send(accepted_key, text)
        assert replay.status_code == 202, replay.text
        assert replay.json()["id"] == turn["id"]
        changed_body = send(accepted_key, text + " Назови источник.")
        assert changed_body.status_code == 409, changed_body.text

        events = http.get(root + f"/chat/runs/{turn['run_id']}/events", timeout=90)
        assert events.status_code == 200, events.text
        assert "event: completed" in events.text, events.text
        history = http.get(root + f"/chat/sessions/{session['id']}").json()
        assert len(history["turns"]) == 1
        assert history["turns"][0]["id"] == turn["id"]
        assert history["turns"][0]["status"] == "completed"


def test_chat_agent_cannot_persist_foreign_source_reference():
    with httpx.Client(base_url="http://frontend", timeout=30, trust_env=False) as http:
        root, _, review = prepare(
            http,
            "РПД/РПД_матанализ1.docx",
            content=make_docx(_paragraph("Тестовая дисциплина: 108 часов")),
        )
        version, _ = choose(http, root, review)
        session = post(
            http,
            root + "/chat/sessions",
            {"version_id": version["id"]},
            201,
        )
        response = http.post(
            root + f"/chat/sessions/{session['id']}/messages",
            json={"text": "fixture-foreign-source: измени часы до 54"},
            headers={"Idempotency-Key": uuid4().hex},
            timeout=30,
        )
        assert response.status_code == 202, response.text
        run_id = response.json()["run_id"]

        events = http.get(root + f"/chat/runs/{run_id}/events", timeout=90)
        assert events.status_code == 200, events.text
        assert "event: failed" in events.text, events.text
        assert "Traceback" not in events.text
        assert "source_ref" not in events.text

        state = http.get(root).json()
        history = http.get(root + f"/chat/sessions/{session['id']}").json()
        assert state["current_version_id"] == version["id"]
        assert history["version_id"] == version["id"]
        assert history["turns"][-1]["status"] == "failed"
        assert history["turns"][-1]["result"] is None


def test_chat_agent_http_temporal_sources_proposal_accept_and_check():
    original_text = "Тестовая дисциплина: исходно 108 часов"
    original = make_docx(_paragraph(original_text))
    with httpx.Client(base_url="http://frontend", timeout=30, trust_env=False) as http:
        root, _, review = prepare(
            http,
            "РПД/РПД_матанализ1.docx",
            content=original,
        )
        program_id = UUID(root.rsplit("/", 1)[-1])
        version, _ = choose(http, root, review)
        session = post(
            http,
            root + "/chat/sessions",
            {"version_id": version["id"]},
            201,
        )

        first_run, first_answer = send_chat_message(
            http,
            root,
            session["id"],
            "Какая исходная трудоёмкость у Тестовая дисциплина? Приведи источник.",
        )
        assert first_answer["kind"] == "answer"
        assert "108" in first_answer["message"]
        assert len(first_answer["sources"]) == 1
        source_id = first_answer["sources"][0]["id"]
        assert source_id in {item["id"] for item in version["snapshot"]["source_refs"]}

        second_run, proposal = send_chat_message(
            http,
            root,
            session["id"],
            "Сократи трудоёмкость Тестовая дисциплина до 54 часов.",
        )
        assert proposal["kind"] == "change_proposal"
        assert (
            proposal["changes"][0]["expected_old_value"]
            == version["snapshot"]["entities"][0]["attributes"]["hours_total"]
        )
        assert proposal["changes"][0]["new_value"] == 54
        assert (
            http.get(root + f"/chat/sessions/{session['id']}").json()["version_id"] == version["id"]
        )

        accepted = post(
            http,
            root + f"/chat/sessions/{session['id']}/runs/{second_run}/accept",
            {
                "expected_version_id": version["id"],
                "signature": "Тестовый пользователь",
            },
            201,
            uuid4().hex,
        )
        accepted_version = accepted["version"]
        assert accepted_version["snapshot"]["entities"][0]["attributes"]["hours_total"] == 54
        checked = wait(
            http,
            root + f"/reviews/{review['id']}/checks/{accepted['check_job_id']}",
        )
        assert checked["status"] == "completed", checked
        assert checked["input_version_id"] == accepted_version["id"]
        assert checked["report"]["coverage"]

        third_run, current_answer = send_chat_message(
            http,
            root,
            session["id"],
            "Сколько часов сохранено в текущем графе у Тестовая дисциплина?",
        )
        assert current_answer["kind"] == "answer"
        assert "54" in current_answer["message"]
        assert current_answer["sources"][0]["id"] == source_id

        fourth_run, original_source_answer = send_chat_message(
            http,
            root,
            session["id"],
            "Сколько часов указано в исходном документе для Тестовая дисциплина?",
        )
        assert original_source_answer["kind"] == "answer"
        assert "108" in original_source_answer["message"]
        assert original_source_answer["sources"][0]["id"] == source_id

        settings = get_settings()
        with psycopg.connect(
            host=settings.postgres_host,
            dbname=settings.postgres_db,
            user=settings.postgres_user,
            password=settings.postgres_password.get_secret_value(),
        ) as conn:
            versions_before_check = conn.execute(
                "SELECT count(*) FROM program_versions WHERE program_id=%s",
                (program_id,),
            ).fetchone()[0]
            graph_state_before_check = conn.execute(
                "SELECT graph_state_id FROM program_versions WHERE id=%s",
                (accepted_version["id"],),
            ).fetchone()[0]

        fifth_run, check_answer = send_chat_message(
            http,
            root,
            session["id"],
            "Проверь программу и сообщи результат.",
        )
        assert check_answer["kind"] == "answer"
        check_job_id = re.search(
            r"задача ([0-9a-f-]{36})", check_answer["message"], flags=re.IGNORECASE
        )
        assert check_job_id, check_answer
        check_result = wait(
            http,
            root + f"/reviews/{review['id']}/checks/{check_job_id.group(1)}",
        )
        assert check_result["status"] == "completed", check_result
        assert check_result["input_version_id"] == accepted_version["id"]
        assert check_result["output_version_id"] is None
        with psycopg.connect(
            host=settings.postgres_host,
            dbname=settings.postgres_db,
            user=settings.postgres_user,
            password=settings.postgres_password.get_secret_value(),
        ) as conn:
            assert (
                conn.execute(
                    "SELECT count(*) FROM program_versions WHERE program_id=%s",
                    (program_id,),
                ).fetchone()[0]
                == versions_before_check
            )
            assert (
                conn.execute(
                    "SELECT graph_state_id FROM program_versions WHERE id=%s",
                    (accepted_version["id"],),
                ).fetchone()[0]
                == graph_state_before_check
            )

        history = http.get(root + f"/chat/sessions/{session['id']}").json()
        assert history["version_id"] == accepted_version["id"]
        assert len(history["turns"]) == 5
        assert [turn["status"] for turn in history["turns"]] == ["completed"] * 5
        assert history["turns"][0]["id"] == first_run
        assert history["turns"][0]["result"] == first_answer
        assert history["turns"][1]["id"] == second_run
        assert history["turns"][1]["result"] == proposal
        assert history["turns"][2]["id"] == third_run
        assert history["turns"][2]["result"] == current_answer
        assert history["turns"][3]["id"] == fourth_run
        assert history["turns"][3]["result"] == original_source_answer
        assert history["turns"][4]["id"] == fifth_run
        assert history["turns"][4]["result"] == check_answer
