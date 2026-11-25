"""Хранение бесед и повторная доставка ходов чата в Temporal."""

import hashlib
from uuid import uuid4

from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from app.config import get_settings
from app.errors import DomainError


class ChatRepository:
    def __init__(self, pool, namespace="education"):
        self.pool, self.namespace = pool, namespace

    def create_session(self, program_id, version_id=None):
        session_id = uuid4()
        with self.pool.connection() as conn, conn.cursor(row_factory=dict_row) as cur:
            program = cur.execute(
                "SELECT current_version_id FROM programs WHERE id=%s", (program_id,)
            ).fetchone()
            if program is None:
                raise DomainError(404, "Программа не найдена")
            version_id = version_id or program["current_version_id"]
            version = cur.execute(
                "SELECT graph_state_id,review FROM program_versions WHERE program_id=%s AND id=%s",
                (program_id, version_id),
            ).fetchone()
            if not version:
                raise DomainError(404, "Версия программы не найдена")
            if (
                version["graph_state_id"] is None
                or (version["review"] or {}).get("graph_ready") is False
            ):
                raise DomainError(409, "Чат станет доступен после сборки графа")
            cur.execute(
                "INSERT INTO chat_sessions(id,program_id,version_id) VALUES(%s,%s,%s)",
                (session_id, program_id, version_id),
            )
        return self.session(program_id, session_id)

    def session(self, program_id, session_id):
        with self.pool.connection() as conn, conn.cursor(row_factory=dict_row) as cur:
            row = cur.execute(
                """SELECT s.id,s.program_id,s.version_id,s.title,s.created_at,s.updated_at,
                          v.graph_state_id,v.review FROM chat_sessions s
                   JOIN program_versions v ON v.program_id=s.program_id AND v.id=s.version_id
                   WHERE s.program_id=%s AND s.id=%s""",
                (program_id, session_id),
            ).fetchone()
            if row is None:
                raise DomainError(404, "Сессия чата не найдена")
            if row["graph_state_id"] is None or (row["review"] or {}).get("graph_ready") is False:
                raise DomainError(409, "Чат станет доступен после сборки графа")
            current_decisions = {
                str(item["conflict_id"])
                for item in (row["review"] or {}).get("decisions", [])
            }
            historical_decisions = {
                item["conflict_id"]
                for item in cur.execute(
                    """WITH RECURSIVE lineage(id,parent_version_id,review) AS (
                           SELECT id,parent_version_id,review FROM program_versions WHERE id=%s
                           UNION ALL
                           SELECT v.id,v.parent_version_id,v.review FROM program_versions v
                           JOIN lineage parent ON v.id=parent.parent_version_id
                       )
                       SELECT DISTINCT decision->>'conflict_id' AS conflict_id
                       FROM lineage CROSS JOIN LATERAL jsonb_array_elements(
                           COALESCE(review->'decisions','[]'::jsonb)
                       ) AS decisions(decision)""",
                    (row["version_id"],),
                ).fetchall()
            }
            row.pop("graph_state_id")
            row.pop("review")
            turns = cur.execute(
                "SELECT id AS run_id,id,user_text,status,result,created_at,completed_at "
                "FROM chat_turns "
                "WHERE session_id=%s ORDER BY created_at,id",
                (session_id,),
            ).fetchall()
            for turn in turns:
                turn["proposal_reverted"] = (
                    turn["result"] is not None
                    and turn["result"].get("kind") == "change_proposal"
                    and str(turn["id"]) in historical_decisions
                    and str(turn["id"]) not in current_decisions
                )
            row["turns"] = turns
            return row

    def sessions(self, program_id, limit=50, offset=0):
        with self.pool.connection() as conn, conn.cursor(row_factory=dict_row) as cur:
            return cur.execute(
                """SELECT s.id,s.program_id,s.version_id,s.title,s.created_at,s.updated_at
                   FROM chat_sessions s JOIN program_versions v
                     ON v.program_id=s.program_id AND v.id=s.version_id
                   WHERE s.program_id=%s AND v.graph_state_id IS NOT NULL
                     AND COALESCE(v.review->>'graph_ready','true')<>'false'
                   ORDER BY s.updated_at DESC,s.id LIMIT %s OFFSET %s""",
                (program_id, limit, offset),
            ).fetchall()

    def create_turn(self, program_id, session_id, key, text):
        fingerprint = hashlib.sha256(text.encode()).hexdigest()
        turn_id = uuid4()
        workflow_id = f"chat-{turn_id}"
        with self.pool.connection() as conn, conn.cursor(row_factory=dict_row) as cur:
            session = cur.execute(
                "SELECT id FROM chat_sessions WHERE program_id=%s AND id=%s FOR UPDATE",
                (program_id, session_id),
            ).fetchone()
            if session is None:
                raise DomainError(404, "Сессия чата не найдена")
            ready = cur.execute(
                """SELECT v.graph_state_id,v.review FROM chat_sessions s
                   JOIN program_versions v ON v.program_id=s.program_id AND v.id=s.version_id
                   WHERE s.id=%s""",
                (session_id,),
            ).fetchone()
            if (
                ready["graph_state_id"] is None
                or (ready["review"] or {}).get("graph_ready") is False
            ):
                raise DomainError(409, "Чат станет доступен после сборки графа")
            existing = cur.execute(
                "SELECT id,request_hash FROM chat_turns WHERE session_id=%s AND operation_key=%s",
                (session_id, key),
            ).fetchone()
            if existing:
                if existing["request_hash"] != fingerprint:
                    raise DomainError(409, "Ключ использован для другого сообщения")
                return existing["id"]
            if cur.execute(
                "SELECT 1 FROM chat_turns WHERE session_id=%s AND status='pending'",
                (session_id,),
            ).fetchone():
                raise DomainError(409, "Дождитесь завершения предыдущего сообщения")
            if (
                cur.execute(
                    "SELECT count(*) AS total FROM chat_turns WHERE session_id=%s", (session_id,)
                ).fetchone()["total"]
                >= get_settings().chat_max_turns
            ):
                raise DomainError(409, "Достигнут предел сообщений в этой сессии")
            cur.execute(
                "INSERT INTO chat_turns(id,session_id,operation_key,request_hash,user_text,"
                "workflow_id) "
                "VALUES(%s,%s,%s,%s,%s,%s)",
                (turn_id, session_id, key, fingerprint, text, workflow_id),
            )
            cur.execute("UPDATE chat_sessions SET updated_at=now() WHERE id=%s", (session_id,))
        return turn_id

    def pending(self, after=None, limit=100):
        with self.pool.connection() as conn, conn.cursor() as cur:
            if after is None:
                query = (
                    "SELECT t.id,t.workflow_id,t.created_at FROM chat_turns t "
                    "WHERE t.status='pending' ORDER BY t.created_at,t.id LIMIT %s"
                )
                params = (limit,)
            else:
                query = (
                    "SELECT t.id,t.workflow_id,t.created_at FROM chat_turns t "
                    "WHERE t.status='pending' AND (t.created_at,t.id)>(%s,%s) "
                    "ORDER BY t.created_at,t.id LIMIT %s"
                )
                params = (*after, limit)
            return cur.execute(query, params).fetchall()

    def turn_input(self, turn_id):
        with self.pool.connection() as conn, conn.cursor(row_factory=dict_row) as cur:
            row = cur.execute(
                "SELECT t.id,t.user_text,t.created_at,s.id AS session_id,s.program_id,s.version_id "
                "FROM chat_turns t JOIN chat_sessions s ON s.id=t.session_id WHERE t.id=%s",
                (turn_id,),
            ).fetchone()
            if row is None:
                raise DomainError(404, "Ход чата не найден")
            version = cur.execute(
                """SELECT v.graph_state_id,v.review FROM chat_sessions s
                   JOIN program_versions v ON v.program_id=s.program_id AND v.id=s.version_id
                   WHERE s.id=%s""",
                (row["session_id"],),
            ).fetchone()
            if (
                version["graph_state_id"] is None
                or (version["review"] or {}).get("graph_ready") is False
            ):
                raise DomainError(409, "Чат станет доступен после сборки графа")
            return row

    def prior_turns(self, turn, limit):
        """Завершённые обмены до сообщения в той же сессии, от старых к новым."""
        with self.pool.connection() as conn, conn.cursor(row_factory=dict_row) as cur:
            rows = cur.execute(
                """SELECT user_text,result FROM chat_turns WHERE session_id=%s
                   AND status='completed' AND result IS NOT NULL
                   AND (created_at,id)<(%s,%s)
                   ORDER BY created_at DESC,id DESC LIMIT %s""",
                (turn["session_id"], turn["created_at"], turn["id"], limit),
            ).fetchall()
        return rows[::-1]

    def turn_status(self, turn_id):
        with self.pool.connection() as conn, conn.cursor(row_factory=dict_row) as cur:
            row = cur.execute("SELECT id,status FROM chat_turns WHERE id=%s", (turn_id,)).fetchone()
            if row is None:
                raise DomainError(404, "Ход чата не найден")
            return row

    def complete(self, turn_id, result, conversation):
        if len(conversation) > get_settings().chat_max_conversation_bytes:
            raise DomainError(413, "История чата превысила допустимый размер")
        turn = self.turn_input(turn_id)
        from app.repository import Repository

        snapshot = Repository(self.pool).version(turn["program_id"], turn["version_id"])["snapshot"]
        sources = {str(item["id"]): item for item in snapshot.get("source_refs", [])}
        source_ids = set(sources)
        entity_ids = {str(item["id"]) for item in snapshot.get("entities", [])}
        relation_ids = {str(item["id"]) for item in snapshot.get("relations", [])}
        if result.get("kind") == "answer":
            if any(str(item["id"]) not in source_ids for item in result.get("sources", [])):
                raise DomainError(422, "Ответ содержит источник вне выбранной версии")
            result["sources"] = [sources[str(item["id"])] for item in result.get("sources", [])]
        elif result.get("kind") == "change_proposal":
            from app.curriculum_workload import with_legacy_curriculum_workload
            from app.domain import SnapshotV2
            from worker.agent.contracts import ChangeProposal
            from worker.agent.proposal_apply import workload_targets

            targets = workload_targets(ChangeProposal.model_validate(result))
            if targets:
                editable = with_legacy_curriculum_workload(
                    SnapshotV2.model_validate(snapshot), turn["program_id"], targets,
                    Repository(self.pool).document_content,
                )
                source_ids.update(str(ref.id) for ref in editable.source_refs)
            for change in result.get("changes", []):
                allowed = entity_ids if change["target_kind"] == "entity" else relation_ids
                if str(change["target_id"]) not in allowed or not set(
                    map(str, change["source_ref_ids"])
                ).issubset(source_ids):
                    raise DomainError(422, "Предложение содержит ссылку вне выбранной версии")
        with self.pool.connection() as conn, conn.cursor() as cur:
            cur.execute(
                "UPDATE chat_turns SET status='completed',result=%s,conversation=%s,"
                "completed_at=now() "
                "WHERE id=%s AND status='pending'",
                (Jsonb(result), conversation, turn_id),
            )

    def fail(self, turn_id):
        with self.pool.connection() as conn, conn.cursor() as cur:
            cur.execute(
                "UPDATE chat_turns SET status='failed',completed_at=now() "
                "WHERE id=%s AND status='pending'",
                (turn_id,),
            )
