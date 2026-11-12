"""Бизнес-задачи анализа, неизменяемые решения и подтверждение выбранной версии."""

import json
from datetime import UTC, datetime
from uuid import UUID, uuid4, uuid5

from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from app.config import get_settings
from app.domain import SnapshotV2
from app.errors import DomainError
from app.repository import Repository
from app.review_contract import CheckResult
from app.review_logic import (
    apply_changes,
    changes_in_base_sources,
    content_hash,
    copy_sources,
    digest,
)
from app.review_storage import insert_issues, insert_version, previous_version


class ReviewRepository:
    def __init__(self, pool):
        self.pool = pool
        self.settings = get_settings()

    @staticmethod
    def lock(cur, program_id):
        cur.execute("SET LOCAL statement_timeout='5s'")
        return Repository.require(
            cur.execute(
                "SELECT current_version_id FROM programs WHERE id=%s FOR UPDATE",
                (program_id,),
            ).fetchone()
        )["current_version_id"]

    @staticmethod
    def version(cur, program_id, version_id):
        return Repository.require(
            cur.execute(
                "SELECT * FROM program_versions WHERE program_id=%s AND id=%s",
                (program_id, version_id),
            ).fetchone()
        )

    @staticmethod
    def require_review(cur, program_id, review_id):
        return Repository.require(
            cur.execute(
                """SELECT * FROM processing_jobs WHERE program_id=%s AND id=%s
               AND type='validate' AND parameters->>'purpose'='prepare'""",
                (program_id, review_id),
            ).fetchone()
        )

    @staticmethod
    def checked_snapshot(row, review_id):
        if row["snapshot"].get("schema_version") != 2:
            raise DomainError(404, "Версия не принадлежит подготовке")
        snapshot = SnapshotV2.model_validate(row["snapshot"])
        if snapshot.review is None or snapshot.review.review_id != review_id:
            raise DomainError(404, "Версия не принадлежит этой подготовке")
        if snapshot.review.confirmation:
            raise DomainError(409, "Подтверждённая версия неизменяема; создайте новую подготовку")
        return snapshot

    def create(self, program_id, key, body, *, purpose="prepare", review_id=None):
        request = body.model_dump(mode="json")
        fingerprint = digest({"operation": purpose, "review_id": str(review_id), "body": request})
        with self.pool.connection() as connection, connection.cursor(row_factory=dict_row) as cur:
            current = self.lock(cur, program_id)
            previous = cur.execute(
                """SELECT id,request_hash,type FROM processing_jobs WHERE program_id=%s
                   AND (operation_key=%s OR parameters->'request_keys' ? %s)""",
                (program_id, key, key),
            ).fetchone()
            if previous:
                if previous["type"] != "validate" or previous["request_hash"] != fingerprint:
                    raise DomainError(409, "Ключ использован для другого запроса")
                return previous["id"]
            if cur.execute(
                "SELECT id FROM program_versions WHERE program_id=%s AND operation_key=%s",
                (program_id, key),
            ).fetchone():
                raise DomainError(409, "Ключ использован для сохранения версии")
            if not self.settings.analysis_url:
                raise DomainError(503, "ML-анализ не настроен; фиктивный результат не создаётся")
            parameters = {"purpose": purpose, "review_id": str(review_id) if review_id else None}
            if purpose == "prepare":
                if current != body.expected_version_id:
                    raise DomainError(409, "Текущая версия изменилась")
                source = Repository.require(
                    cur.execute(
                        """SELECT * FROM processing_jobs WHERE program_id=%s AND id=%s
                       AND type='import' FOR SHARE""",
                        (program_id, body.import_job_id),
                    ).fetchone()
                )
                report = source["report"]
                files = []
                for member in source["parameters"]["documents"]:
                    result = report.get("files", {}).get(member["document_id"], {})
                    if not result.get("output") or result.get("error_code"):
                        raise DomainError(409, "Не все файлы пакета успешно разобраны")
                    if any(
                        issue["severity"] == "error"
                        for issue in result["output"].get("parse_issues", [])
                    ):
                        raise DomainError(409, "Пакет содержит ошибки разбора")
                    document = Repository.require(
                        cur.execute(
                            "SELECT id,sha256 FROM source_documents WHERE program_id=%s AND id=%s",
                            (program_id, UUID(member["document_id"])),
                        ).fetchone()
                    )
                    files.append(
                        {
                            "document_id": str(document["id"]),
                            "sha256": document["sha256"],
                            "parsed": result["output"],
                        }
                    )
                    files[-1]["original_url"] = (
                        self.settings.analysis_original_base_url.rstrip("/")
                        + f"/api/v1/programs/{program_id}/documents/{document['id']}/download"
                    )
                if not files or report.get("outcome") not in {"completed", "partial"}:
                    raise DomainError(409, "Импорт не завершён")
                base = self.version(cur, program_id, current)["snapshot"]
                parameters.update(
                    import_job_id=str(body.import_job_id),
                    import_workflow_id=source["workflow_id"],
                    input={"base_snapshot": base, "documents": files},
                )
                version_id = current
            else:
                review = self.require_review(cur, program_id, review_id)
                if body.version_id != current:
                    raise DomainError(409, "Проверять нужно текущую версию")
                snapshot = self.checked_snapshot(self.version(cur, program_id, current), review_id)
                ruleset = review["report"]["result"]["ruleset"]
                parameters.update(
                    content_hash=content_hash(snapshot),
                    ruleset=ruleset,
                    input={
                        "snapshot": snapshot.model_dump(mode="json"),
                        "ruleset": ruleset,
                        "documents": review["parameters"]["input"]["documents"],
                    },
                )
                version_id = current
            job_id = uuid4()
            size = len(json.dumps(parameters, ensure_ascii=False, allow_nan=False).encode())
            if size > self.settings.analysis_max_request_bytes:
                raise DomainError(413, "Вход анализа превышает настроенный предел JSON")
            if purpose == "prepare":
                parameters["review_id"] = str(job_id)
            cur.execute(
                """INSERT INTO processing_jobs(id,program_id,input_version_id,operation_key,
                   request_hash,type,temporal_namespace,workflow_id,parameters)
                   VALUES(%s,%s,%s,%s,%s,'validate',%s,%s,%s)""",
                (
                    job_id,
                    program_id,
                    version_id,
                    key,
                    fingerprint,
                    self.settings.temporal_namespace,
                    f"review-{job_id}",
                    Jsonb(parameters),
                ),
            )
            return job_id

    def job(self, program_id, job_id):
        with self.pool.connection() as connection, connection.cursor(row_factory=dict_row) as cur:
            return Repository.require(
                cur.execute(
                    """SELECT * FROM processing_jobs WHERE program_id=%s AND id=%s
                   AND type='validate' AND parameters->>'purpose' IN ('prepare','check')""",
                    (program_id, job_id),
                ).fetchone()
            )

    def job_by_id(self, job_id):
        with self.pool.connection() as connection:
            program = Repository.require(
                connection.execute(
                    "SELECT program_id FROM processing_jobs WHERE id=%s AND type='validate'",
                    (job_id,),
                ).fetchone()
            )[0]
        return self.job(program, job_id)

    def pending(self):
        with self.pool.connection() as connection:
            return connection.execute(
                """SELECT id,workflow_id FROM processing_jobs WHERE type='validate'
                   AND parameters->>'purpose' IN ('prepare','check') AND NOT report ? 'outcome'
                   ORDER BY created_at,id""",
            ).fetchall()

    def listing(self, program_id, limit, offset):
        Repository(self.pool).program(program_id)
        with self.pool.connection() as connection:
            return [
                row[0]
                for row in connection.execute(
                    """SELECT id FROM processing_jobs WHERE program_id=%s AND type='validate'
                   AND parameters->>'purpose'='prepare'
                   ORDER BY created_at DESC,id LIMIT %s OFFSET %s""",
                    (program_id, limit, offset),
                )
            ]

    def publish(self, job_id, result, artifacts=(), raw_result=None, raw_hash=None):
        job = self.job_by_id(job_id)
        program_id, purpose = job["program_id"], job["parameters"]["purpose"]
        with self.pool.connection() as connection, connection.cursor(row_factory=dict_row) as cur:
            current = self.lock(cur, program_id)
            locked = cur.execute(
                "SELECT * FROM processing_jobs WHERE id=%s FOR UPDATE", (job_id,)
            ).fetchone()
            if locked["workflow_id"] != job["workflow_id"] or "outcome" in locked["report"]:
                return locked["report"].get("outcome", "failed")
            if purpose == "prepare" and current != job["input_version_id"]:
                report = {
                    "outcome": "conflict",
                    "result": result,
                    "raw_result": raw_result,
                    "raw_result_hash": raw_hash,
                    "message": "Версия изменилась во время анализа; результат сохранён",
                }
                cur.execute(
                    "UPDATE processing_jobs SET report=%s WHERE id=%s", (Jsonb(report), job_id)
                )
                return "conflict"
            output_id = None
            if purpose == "prepare":
                for artifact in artifacts:
                    cur.execute(
                        """INSERT INTO document_artifacts(
                           id,program_id,document_id,producing_job_id,
                           parser_revision,parser_config_hash,storage_key,sha256,size_bytes)
                           VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
                        (
                            UUID(artifact["id"]),
                            program_id,
                            UUID(artifact["document_id"]),
                            job_id,
                            artifact["parser_revision"],
                            artifact["parser_config_hash"],
                            artifact["id"],
                            artifact["sha256"],
                            artifact["size_bytes"],
                        ),
                    )
                output_id = uuid5(job_id, "analysis-version")
                original = SnapshotV2.model_validate(result["candidate"])
                snapshot = copy_sources(original, output_id)
                refs = {
                    str(a.id): str(b.id)
                    for a, b in zip(original.source_refs, snapshot.source_refs, strict=True)
                }
                for issue in result["issues"]:
                    issue["source_ref_ids"] = [refs[key] for key in issue["source_ref_ids"]]
                    for variant in issue["variants"]:
                        for change in variant["changes"]:
                            for item_key in ("entity", "relation"):
                                item = change.get(item_key)
                                if item:
                                    item["source_ref_ids"] = [
                                        refs[key] for key in item["source_ref_ids"]
                                    ]
                state = snapshot.model_dump(mode="json")
                state["review"] = {
                    "review_id": str(job_id),
                    "import_job_id": job["parameters"]["import_job_id"],
                    "analysis_version_id": str(output_id),
                    "decisions": [],
                    "confirmation": None,
                }
                snapshot = SnapshotV2.model_validate(state)
                result["candidate"] = snapshot.model_dump(mode="json")
                insert_version(cur, program_id, current, output_id, snapshot)
                issue_version = output_id
            else:
                issue_version = job["input_version_id"]
                CheckResult.model_validate(result)
                if digest(result["ruleset"]) != digest(job["parameters"]["ruleset"]):
                    raise ValueError("Check used another ruleset")
            insert_issues(cur, program_id, job_id, issue_version, result["issues"])
            report = {
                "outcome": "completed",
                "result": result,
                "raw_result": raw_result,
                "raw_result_hash": raw_hash,
                "completed_at": datetime.now(UTC).isoformat(),
            }
            cur.execute(
                "UPDATE processing_jobs SET output_version_id=%s,report=%s WHERE id=%s",
                (output_id, Jsonb(report), job_id),
            )
            return "completed"

    def fail(self, job_id, workflow_id, code, message):
        with self.pool.connection() as connection:
            connection.execute(
                """UPDATE processing_jobs SET report=%s WHERE id=%s AND workflow_id=%s
                   AND NOT report ? 'outcome'""",
                (
                    Jsonb({"outcome": "failed", "code": code, "message": message}),
                    job_id,
                    workflow_id,
                ),
            )

    def decide(self, program_id, review_id, key, body):
        fingerprint = digest(
            {
                "operation": "decision",
                "review_id": str(review_id),
                "body": body.model_dump(mode="json"),
            }
        )
        with self.pool.connection() as connection, connection.cursor(row_factory=dict_row) as cur:
            current = self.lock(cur, program_id)
            previous = previous_version(cur, program_id, key, fingerprint)
            if previous:
                return previous
            review = self.require_review(cur, program_id, review_id)
            if current != body.expected_version_id:
                raise DomainError(409, "Текущая версия изменилась")
            snapshot = self.checked_snapshot(self.version(cur, program_id, current), review_id)
            issue = Repository.require(
                cur.execute(
                    "SELECT job_id,version_id,details FROM validation_issues "
                    "WHERE program_id=%s AND id=%s",
                    (program_id, body.conflict_id),
                ).fetchone()
            )
            owner = self.job_with_cursor(cur, program_id, issue["job_id"])
            if owner["parameters"]["review_id"] != str(review_id):
                raise DomainError(404, "Конфликт из другой подготовки")
            if body.check_job_id and body.check_job_id != issue["job_id"]:
                raise DomainError(404, "Конфликт из другой проверки")
            if issue["job_id"] != review_id and issue["version_id"] != current:
                raise DomainError(409, "Конфликт относится к прежней редакции")
            details = issue["details"]
            variant = next(
                (v for v in details["variants"] if v["id"] == str(body.variant_id)), None
            )
            if not details["requires_decision"] or variant is None:
                raise DomainError(422, "Выберите один из предложенных вариантов")
            base = review["report"]["result"]["candidate"]
            source_snapshot = self.version(cur, program_id, issue["version_id"])["snapshot"]
            changes = changes_in_base_sources(variant["changes"], source_snapshot, base)
            choices = [
                item.model_dump(mode="json")
                for item in snapshot.review.decisions
                if item.conflict_id != body.conflict_id
            ]
            choices.append(
                {
                    "conflict_id": str(body.conflict_id),
                    "variant_id": str(body.variant_id),
                    "check_job_id": str(issue["job_id"]),
                    "changes": changes,
                    "decided_at": datetime.now(UTC).isoformat(),
                    "author": None,
                }
            )
            selected = apply_changes(
                base, [change for choice in choices for change in choice["changes"]]
            )
            state = selected.model_dump(mode="json")
            if state.get("review") is None:
                state["review"] = snapshot.review.model_dump(mode="json")
            state["review"]["decisions"] = choices
            selected = SnapshotV2.model_validate(state)
            version_id = uuid4()
            selected = copy_sources(selected, version_id)
            return insert_version(
                cur, program_id, current, version_id, selected, key=key, fingerprint=fingerprint
            )

    @staticmethod
    def job_with_cursor(cur, program_id, job_id):
        return Repository.require(
            cur.execute(
                "SELECT * FROM processing_jobs WHERE program_id=%s AND id=%s AND type='validate'",
                (program_id, job_id),
            ).fetchone()
        )

    def confirm(self, program_id, review_id, key, body):
        fingerprint = digest(
            {
                "operation": "confirm",
                "review_id": str(review_id),
                "body": body.model_dump(mode="json"),
            }
        )
        with self.pool.connection() as connection, connection.cursor(row_factory=dict_row) as cur:
            current = self.lock(cur, program_id)
            previous = previous_version(cur, program_id, key, fingerprint)
            if previous:
                return previous
            review = self.require_review(cur, program_id, review_id)
            if current != body.expected_version_id:
                raise DomainError(409, "Текущая версия изменилась")
            snapshot = self.checked_snapshot(self.version(cur, program_id, current), review_id)
            checked = self.job_with_cursor(cur, program_id, body.check_job_id)
            if any(not item.source_ref_ids for item in [*snapshot.entities, *snapshot.relations]):
                raise DomainError(
                    409, "У подтверждаемых сущностей или связей отсутствуют источники"
                )
            if checked["parameters"].get("review_id") != str(review_id):
                raise DomainError(404, "Проверка из другой подготовки")
            if (
                checked["parameters"].get("purpose") != "check"
                or checked["input_version_id"] != current
                or checked["report"].get("outcome") != "completed"
                or checked["parameters"].get("content_hash") != content_hash(snapshot)
            ):
                raise DomainError(409, "Нужна завершённая проверка точной текущей версии")
            result = CheckResult.model_validate(checked["report"]["result"])
            if digest(result.ruleset.model_dump(mode="json")) != digest(
                checked["parameters"]["ruleset"]
            ):
                raise DomainError(409, "Набор правил проверки изменился")
            coverage = {item.rule_code: item.status for item in result.coverage}
            if any(
                coverage[code] not in {"passed", "failed"} for code in result.ruleset.required_rules
            ):
                raise DomainError(409, "Обязательные проверки пропущены")
            decided = {str(item.conflict_id) for item in snapshot.review.decisions}
            original = review["report"]["result"]["issues"]
            if any(i["requires_decision"] and i["id"] not in decided for i in original):
                raise DomainError(409, "Остались конфликты без решения")
            if any(item.requires_decision or item.severity == "blocking" for item in result.issues):
                raise DomainError(409, "Остались блокирующие замечания или новые конфликты")
            warnings = {item.id for item in result.issues if item.severity == "warning"}
            accepted = set(body.accepted_warning_ids)
            if accepted != warnings or len(accepted) != len(body.accepted_warning_ids):
                raise DomainError(409, "Нужно явно принять актуальные предупреждения")
            hash_before = content_hash(snapshot)
            state = snapshot.model_dump(mode="json")
            state["review"]["confirmation"] = {
                "checked_version_id": str(current),
                "check_job_id": str(body.check_job_id),
                "content_hash": hash_before,
                "accepted_warning_ids": [str(v) for v in body.accepted_warning_ids],
                "confirmed_at": datetime.now(UTC).isoformat(),
                "author": None,
            }
            snapshot = SnapshotV2.model_validate(state)
            version_id = uuid4()
            snapshot = copy_sources(snapshot, version_id)
            if content_hash(snapshot) != hash_before:
                raise DomainError(409, "Содержимое не совпадает с проверенным")
            return insert_version(
                cur, program_id, current, version_id, snapshot, key=key, fingerprint=fingerprint
            )

    def details(self, program_id, version_id, limit, offset):
        row = Repository(self.pool).version(program_id, version_id)
        if row["snapshot"].get("schema_version") != 2:
            raise DomainError(404, "У версии нет контекста подготовки")
        snapshot = SnapshotV2.model_validate(row["snapshot"])
        if snapshot.review is None:
            raise DomainError(404, "У версии нет контекста подготовки")
        review = self.job(program_id, snapshot.review.review_id)
        choices = {
            str(item.conflict_id): item.model_dump(mode="json")
            for item in snapshot.review.decisions
        }
        with self.pool.connection() as connection, connection.cursor(row_factory=dict_row) as cur:
            if snapshot.review.confirmation:
                latest = self.job_with_cursor(
                    cur, program_id, snapshot.review.confirmation.check_job_id
                )
            else:
                latest = cur.execute(
                    """SELECT * FROM processing_jobs WHERE program_id=%s AND input_version_id=%s
                       AND type='validate' AND parameters->>'purpose'='check'
                       AND report->>'outcome'='completed'
                       ORDER BY created_at DESC,id DESC LIMIT 1""",
                    (program_id, version_id),
                ).fetchone()
            job_ids = [review["id"]] + ([latest["id"]] if latest else [])
            stored = cur.execute(
                """SELECT job_id,version_id,details FROM validation_issues
                   WHERE program_id=%s AND job_id=ANY(%s) ORDER BY created_at,id""",
                (program_id, job_ids),
            ).fetchall()
        issues = [
            {**item["details"], "source_version_id": item["version_id"], "job_id": item["job_id"]}
            for item in stored
        ]
        accepted = (
            set(snapshot.review.confirmation.accepted_warning_ids)
            if snapshot.review.confirmation
            else set()
        )
        return {
            "version": row,
            "conflicts_count": sum(i["requires_decision"] for i in issues),
            "issues_count": len(issues),
            "issues": [
                {
                    **item,
                    "effective_status": "resolved"
                    if item["id"] in choices
                    else ("accepted" if UUID(item["id"]) in accepted else item["status"]),
                    "decision": choices.get(item["id"]),
                }
                for item in issues[offset : offset + limit]
            ],
            "confirmed": snapshot.review.confirmation is not None,
            "confirmation": snapshot.review.confirmation,
            "check_job_id": latest["id"] if latest else None,
            "check_report": latest["report"].get("result") if latest else None,
        }
