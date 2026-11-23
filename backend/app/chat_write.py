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
    touched_document_ids,
    unwritable_changes,
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
                except ProposalApplicationError as error:
                    raise DomainError(422, str(error)) from error
                # Правка, которую нельзя записать в исходный документ (значение
                # вычислено из других ячеек, текст размазан по разным форматам),
                # не должна доживать до генерации после подтверждения версии.
                touched = sorted(touched_document_ids(snapshot, changes))
                rows = (
                    cur.execute(
                        "SELECT id,content FROM source_documents "
                        "WHERE program_id=%s AND id=ANY(%s)",
                        (program_id, touched),
                    ).fetchall()
                    if touched
                    else []
                )
                problems = unwritable_changes(
                    snapshot, changes, {str(row["id"]): bytes(row["content"]) for row in rows}
                )
                if problems:
                    missing = any("не найдено в документе" in item for item in problems)
                    raise DomainError(
                        422,
                        "Правку нельзя записать в исходные документы:\n"
                        + "\n".join(f"• {item}" for item in problems)
                        + (
                            "\nСкорее всего, значение вычислено из других ячеек документа — "
                            "измените их."
                            if missing
                            else ""
                        ),
                    )
                from app.domain import ReviewDecision

                review = ReviewRepository.require_review(
                    cur, program_id, snapshot.review.review_id
                )
                document_inputs = review["parameters"]["input"]["documents"]
                parent_version_id = current
                prior_changes = []
                for index, change in enumerate(changes):
                    snapshot = apply_change(snapshot, change)
                    prior_changes.append({
                        "op": "set_attribute", "target_kind": change.target_kind,
                        "target_id": str(change.target_id), "field_path": change.field_path,
                        "value": change.new_value,
                    })
                    snapshot.review.confirmation = None
                    snapshot.review.decisions = [
                        item for item in snapshot.review.decisions
                        if item.conflict_id != turn_id
                    ]
                    snapshot.review.decisions.append(ReviewDecision(
                        conflict_id=turn_id, variant_id=turn_id,
                        check_job_id=snapshot.review.review_id,
                        changes=list(prior_changes),
                        decided_at=datetime.now(UTC), author=body.signature,
                    ))
                    # Каждая правка получает собственную неизменяемую версию и проверку.
                    try:
                        project_snapshot_documents(document_inputs, snapshot)
                    except ValueError as error:
                        raise DomainError(422, str(error)) from error
                    version = insert_version(
                        cur, program_id, parent_version_id, uuid4(), snapshot,
                        key=key if index == len(changes) - 1 else None,
                        fingerprint=fingerprint if index == len(changes) - 1 else None,
                        actor_type="user", actor_id=body.signature,
                    )
                    parent_version_id = version["id"]
                    check_id = ReviewRepository(self.pool).create(
                        program_id, "chat-check:" + str(version["id"]),
                        CheckCreate(version_id=version["id"]), purpose="check",
                        review_id=snapshot.review.review_id, allow_historical=True,
                        connection=conn,
                    )
                cur.execute(
                    "UPDATE chat_sessions SET version_id=%s,updated_at=now() WHERE id=%s",
                    (version["id"], session_id),
                )
            else:
                check_id = None
            review_id = UUID(str(version["snapshot"]["review"]["review_id"]))
            if check_id is None:
                check_id = ReviewRepository(self.pool).create(
                    program_id, "chat-check:" + str(version["id"]),
                    CheckCreate(version_id=version["id"]), purpose="check",
                    review_id=review_id, allow_historical=True, connection=conn,
                )
        return {"version": version, "check_job_id": check_id}
