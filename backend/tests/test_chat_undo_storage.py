"""Настоящий PostgreSQL: откат предложения сохраняет граф и продолжение чата."""

import os
from datetime import UTC, datetime
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient
from psycopg.types.json import Jsonb

from app.chat_repository import ChatRepository
from app.domain import Entity, ReviewDecision, ReviewState, SnapshotV2
from app.main import app
from app.repository import Repository

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(os.getenv("RUN_INTEGRATION") != "1", reason="Нужен тестовый PostgreSQL"),
]


def test_undo_chat_proposal_restores_version_and_keeps_session_ready():
    with TestClient(app) as http:
        pool = app.state.resources.postgres
        programs = Repository(pool)
        chats = ChatRepository(pool)
        program = programs.create_program("Проверка отката предложения чата")
        entity_id, review_id, import_job_id, analysis_version_id = [uuid4() for _ in range(4)]
        base_snapshot = SnapshotV2(
            entities=[Entity(id=entity_id, kind="discipline", attributes={"hours_total": 144})],
            review=ReviewState(
                review_id=review_id,
                import_job_id=import_job_id,
                analysis_version_id=analysis_version_id,
            ),
        )
        base = programs.save(
            program["id"], program["current_version_id"], uuid4().hex, base_snapshot
        )
        session = chats.create_session(program["id"], base["id"])
        turn_id = uuid4()
        proposal = {
            "kind": "change_proposal",
            "summary": "Изменить часы",
            "changes": [],
            "unresolved_questions": [],
        }
        decision = ReviewDecision(
            conflict_id=turn_id,
            variant_id=turn_id,
            check_job_id=review_id,
            changes=[{"op": "set_attribute", "value": 160}],
            decided_at=datetime.now(UTC),
        )
        applied_snapshot = base_snapshot.model_copy(update={
            "entities": [Entity(
                id=entity_id, kind="discipline", attributes={"hours_total": 160}
            )],
            "review": base_snapshot.review.model_copy(update={"decisions": [decision]}),
        })
        applied = programs.save(
            program["id"], base["id"], uuid4().hex, applied_snapshot
        )
        with pool.connection() as conn:
            conn.execute(
                "UPDATE chat_sessions SET version_id=%s WHERE id=%s",
                (applied["id"], session["id"]),
            )
            conn.execute(
                "INSERT INTO chat_turns(id,session_id,operation_key,request_hash,user_text,"
                "workflow_id,status,result,completed_at) "
                "VALUES(%s,%s,%s,%s,%s,%s,'completed',%s,now())",
                (
                    turn_id, session["id"], str(turn_id), "0" * 64, "Изменить часы",
                    "chat-" + str(turn_id), Jsonb(proposal),
                ),
            )

        path = (
            f"/api/v1/programs/{program['id']}/chat/sessions/{session['id']}"
            f"/runs/{turn_id}/undo"
        )
        body = {
            "expected_version_id": str(applied["id"]),
            "signature": "Тестовый пользователь",
        }
        key = uuid4().hex
        response = http.post(
            path, json=body, headers={"Idempotency-Key": key},
        )
        assert response.status_code == 201, response.text
        restored = response.json()
        assert restored["snapshot"]["entities"][0]["attributes"]["hours_total"] == 144
        assert restored["snapshot"]["review"]["review_id"] == str(review_id)
        replay = http.post(path, json=body, headers={"Idempotency-Key": key})
        assert replay.status_code == 201, replay.text
        assert replay.json()["id"] == restored["id"]

        later = programs.save(
            program["id"], UUID(restored["id"]), uuid4().hex,
            SnapshotV2.model_validate(restored["snapshot"]),
        )
        with pool.connection() as conn:
            conn.execute(
                "UPDATE chat_sessions SET version_id=%s WHERE id=%s",
                (later["id"], session["id"]),
            )
        replay = http.post(path, json=body, headers={"Idempotency-Key": key})
        assert replay.status_code == 201, replay.text
        assert replay.json()["id"] == restored["id"]
        history = chats.session(program["id"], session["id"])
        assert history["version_id"] == later["id"]

        assert history["turns"][0]["proposal_reverted"] is True
        next_turn = chats.create_turn(program["id"], session["id"], uuid4().hex, "Продолжим")
        assert next_turn
