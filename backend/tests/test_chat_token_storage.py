"""Живой PostgreSQL: число ходов больше не закрывает сессию."""

import os
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from psycopg.types.json import Jsonb

from app.chat_repository import ChatRepository
from app.main import app
from app.repository import Repository

pytestmark = [pytest.mark.integration,
              pytest.mark.skipif(os.getenv("RUN_INTEGRATION") != "1", reason="Нужен PostgreSQL")]


def test_long_session_accepts_new_turn_and_pages_all_prior_history():
    with TestClient(app):
        pool = app.state.resources.postgres
        repo = ChatRepository(pool)
        program = Repository(pool).create_program("Проверка токенного контекста")
        session = repo.create_session(program["id"], program["current_version_id"])
        with pool.connection() as conn:
            with conn.cursor() as cur:
                cur.executemany(
                    "INSERT INTO chat_turns(id,session_id,operation_key,request_hash,user_text,"
                    "workflow_id,status,result) VALUES(%s,%s,%s,%s,%s,%s,'completed',%s)",
                    [(uuid4(), session["id"], str(i), "a" * 64, f"Вопрос {i}", str(uuid4()),
                      Jsonb({"kind": "answer", "message": f"Ответ {i}", "sources": []}))
                     for i in range(105)],
                )
        turn_id = repo.create_turn(program["id"], session["id"], "next", "Новый вопрос")
        turn = repo.turn_input(turn_id)
        latest = repo.prior_turns(turn, 100)
        older = repo.prior_turns(turn, 100, 100)
        assert len(latest) == 100
        assert len(older) == 5
        assert len(repo.session(program["id"], session["id"])["turns"]) == 106
