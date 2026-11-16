"""Принятие сохранённого предложения чата с созданием неизменяемой версии."""

from datetime import UTC, datetime
from uuid import UUID, uuid4

from psycopg.rows import dict_row

from app.domain import SnapshotV2
from app.errors import DomainError
from app.repository import Repository
from app.review_contract import CheckCreate
from app.review_logic import content_hash, digest
from app.review_repository import ReviewRepository
from app.review_storage import insert_version, previous_version
from app.validation_bridge import project_snapshot_documents
from worker.agent.contracts import ChangeProposal
from worker.agent.proposal_apply import (
    ProposalApplicationError,
    apply_change,
    plan_approved_changes,
)


class ChatWriteRepository:
    def __init__(self, pool):
        self.pool = pool

    def accept(self, program_id, session_id, turn_id, body, key):
        fingerprint = digest({
            "operation": "accept_chat_proposal", "session_id": str(session_id),
            "turn_id": str(turn_id), "body": body.model_dump(mode="json"),
        })
        with self.pool.connection() as conn, conn.cursor(row_factory=dict_row) as cur:
            current = ReviewRepository.lock(cur, program_id)
            turn = Repository.require(cur.execute(
                "SELECT t.status,t.result,s.version_id FROM chat_turns t "
                "JOIN chat_sessions s ON s.id=t.session_id "
                "WHERE s.program_id=%s AND s.id=%s AND t.id=%s FOR UPDATE OF s",
                (program_id, session_id, turn_id),
            ).fetchone())
            version = previous_version(cur, program_id, key, fingerprint)
            if version is None:
                if current != body.expected_version_id:
                    raise DomainError(409, "Версия изменилась; получите новое предложение")
                session_snapshot = ReviewRepository.version(
                    cur, program_id, turn["version_id"]
                )["snapshot"]
                current_snapshot = ReviewRepository.version(cur, program_id, current)["snapshot"]
                if content_hash(session_snapshot) != content_hash(current_snapshot):
                    raise DomainError(409, "Предложение относится к другому состоянию графа")
                if turn["status"] != "completed" or (turn["result"] or {}).get(
                    "kind"
                ) != "change_proposal":
                    raise DomainError(409, "Нет завершённого предложения для принятия")
                if cur.execute(
                    "SELECT 1 FROM chat_turns WHERE session_id=%s AND status='pending'",
                    (session_id,),
                ).fetchone():
                    raise DomainError(409, "Дождитесь завершения текущего сообщения")
                snapshot = SnapshotV2.model_validate(
                    current_snapshot
                )
                if snapshot.review is None or not snapshot.review.graph_ready:
                    raise DomainError(409, "Нужен подготовленный граф программы")
                if any(choice.conflict_id == turn_id for choice in snapshot.review.decisions):
                    raise DomainError(409, "Предложение уже принято")
                proposal = ChangeProposal.model_validate(turn["result"])
                if proposal.unresolved_questions:
                    raise DomainError(409, "Сначала разрешите вопросы предложения")
                try:
                    changes = plan_approved_changes(snapshot, proposal)
                    for change in changes:
                        snapshot = apply_change(snapshot, change)
                except ProposalApplicationError as error:
                    raise DomainError(422, str(error)) from error
                snapshot.review.confirmation = None
                # История выбора документов сохраняется; генерация сравнивает итог с оригиналом.
                from app.domain import ReviewDecision

                snapshot.review.decisions.append(ReviewDecision(
                    conflict_id=turn_id, variant_id=turn_id,
                    check_job_id=snapshot.review.review_id,
                    changes=[{
                        "op": "set_attribute", "target_kind": change.target_kind,
                        "target_id": str(change.target_id), "field_path": change.field_path,
                        "value": change.new_value,
                    } for change in changes],
                    decided_at=datetime.now(UTC), author=body.signature,
                ))
                review = ReviewRepository.require_review(
                    cur, program_id, snapshot.review.review_id
                )
                # Отказ до записи версии, если правку нельзя отобразить обратно в документы.
                try:
                    project_snapshot_documents(review["parameters"]["input"]["documents"], snapshot)
                except ValueError as error:
                    raise DomainError(422, str(error)) from error
                version = insert_version(
                    cur, program_id, current, uuid4(), snapshot,
                    key=key, fingerprint=fingerprint,
                    actor_type="user", actor_id=body.signature,
                )
                cur.execute(
                    "UPDATE chat_sessions SET version_id=%s,updated_at=now() WHERE id=%s",
                    (version["id"], session_id),
                )
            # Версия и долговечное задание проверки фиксируются одной транзакцией.
            review_id = UUID(str(version["snapshot"]["review"]["review_id"]))
            check_id = ReviewRepository(self.pool).create(
                program_id, "chat-check:" + str(version["id"]),
                CheckCreate(version_id=version["id"]), purpose="check", review_id=review_id,
                allow_historical=True, connection=conn,
            )
        return {"version": version, "check_job_id": check_id}
