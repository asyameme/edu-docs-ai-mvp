"""Бизнес-задачи анализа, неизменяемые решения и подтверждение выбранной версии."""

import json
from contextlib import nullcontext
from datetime import UTC, datetime
from uuid import UUID, uuid4, uuid5

from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from app.config import get_settings
from app.document_reconciliation import apply_document_mutations
from app.domain import SnapshotV2
from app.errors import DomainError
from app.graph_storage import hydrate_job, pack_tree
from app.repository import Repository
from app.review_contract import CheckResult
from app.review_logic import (
    apply_changes,
    changes_in_base_sources,
    content_hash,
    digest,
)
from app.review_projection import (
    project_legacy_review_issue,
    project_legacy_review_result,
    project_prepare_report,
)
from app.review_storage import insert_issues, insert_version, previous_version


def attach_document_artifacts(snapshot_data, artifacts):
    by_document = {str(item["document_id"]): str(item["id"]) for item in artifacts}
    for source in snapshot_data["source_refs"]:
        artifact_id = by_document.get(str(source["document_id"]))
        if artifact_id:
            source["artifact_id"] = artifact_id
            source["artifact_locator"] = "document"
    return snapshot_data


def graph_build_issues(issues):
    return [
        {
            "category": item["category"],
            "document_id": str(item["document_id"]),
            "message": item["message"],
        }
        for item in issues
        if item.get("category") == "unresolved_relation"
    ]


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
                "SELECT *,graph_snapshot(graph_state_id,review) AS snapshot FROM "
                "program_versions WHERE program_id=%s AND id=%s",
                (program_id, version_id),
            ).fetchone()
        )

    @staticmethod
    def require_review(cur, program_id, review_id):
        return hydrate_job(
            cur,
            Repository.require(
                cur.execute(
                    """SELECT * FROM processing_jobs WHERE program_id=%s AND id=%s
               AND type='validate' AND parameters->>'purpose'='prepare'""",
                    (program_id, review_id),
                ).fetchone()
            ),
        )

    @staticmethod
    def checked_snapshot(row, review_id, *, require_graph=True):
        if row["snapshot"].get("schema_version") != 2:
            raise DomainError(404, "Версия не принадлежит подготовке")
        snapshot = SnapshotV2.model_validate(row["snapshot"])
        if snapshot.review is None or snapshot.review.review_id != review_id:
            raise DomainError(404, "Версия не принадлежит этой подготовке")
        if require_graph and not snapshot.review.graph_ready:
            raise DomainError(409, "Граф ещё не собран из выбранных значений документов")
        if snapshot.review.confirmation:
            raise DomainError(409, "Подтверждённая версия неизменяема; создайте новую подготовку")
        return snapshot

    def create(
        self, program_id, key, body, *, purpose="prepare", review_id=None,
        allow_historical=False,
        connection=None,
    ):
        request = body.model_dump(mode="json", exclude_none=True)
        fingerprint = digest({"operation": purpose, "review_id": str(review_id), "body": request})
        with (
            nullcontext(connection) if connection is not None else self.pool.connection()
        ) as connection, connection.cursor(row_factory=dict_row) as cur:
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
            if (
                not self.settings.analysis_url
                and not self.settings.local_validation_enabled
            ):
                raise DomainError(503, "ML-анализ не настроен; фиктивный результат не создаётся")
            executor = "http" if self.settings.analysis_url else "local"
            parameters = {
                "purpose": purpose,
                "review_id": str(review_id) if review_id else None,
                "executor": executor,
            }
            if purpose == "prepare":
                if current != body.expected_version_id:
                    raise DomainError(409, "Текущая версия изменилась")
                current_snapshot = self.version(cur, program_id, current)["snapshot"]
                current_review = current_snapshot.get("review") or {}
                if current_review.get("graph_ready") is False:
                    previous_review = self.require_review(
                        cur, program_id, UUID(current_review["review_id"])
                    )
                    same_import_rerun = (
                        body.rerun_same_import
                        and str(body.import_job_id) == str(current_review.get("import_job_id"))
                        and str(body.import_job_id)
                        == str(previous_review["parameters"].get("import_job_id"))
                    )
                    if not same_import_rerun:
                        if not previous_review["report"].get("result", {}).get(
                            "graph_build_issues"
                        ):
                            raise DomainError(409, "Сначала завершите согласование текущего пакета")
                        required = {
                            item["id"]
                            for item in previous_review["report"].get("result", {}).get("issues", [])
                            if item.get("requires_decision")
                        }
                        decided = {
                            str(item["conflict_id"]) for item in current_review["decisions"]
                        }
                        if not required.issubset(decided):
                            raise DomainError(409, "Сначала завершите обязательные решения")
                source = Repository.require(
                    cur.execute(
                        """SELECT * FROM processing_jobs WHERE program_id=%s AND id=%s
                       AND type='import' FOR SHARE""",
                        (program_id, body.import_job_id),
                    ).fetchone()
                )
                source = hydrate_job(cur, source)
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
                parameters.update(
                    import_job_id=str(body.import_job_id),
                    import_workflow_id=source["workflow_id"],
                    schema_version=2,
                    input={"documents": files},
                )
                if executor == "local":
                    from app.validation_bridge import validation_implementation_revision
                    from worker.validation.validator import load_rules

                    parameters["local_rules"] = load_rules()
                    parameters["implementation_revision"] = validation_implementation_revision()
                version_id = current
            else:
                review = self.require_review(cur, program_id, review_id)
                executor = review["parameters"].get("executor", "http")
                parameters["executor"] = executor
                if body.version_id != current and not allow_historical:
                    raise DomainError(409, "Проверять нужно текущую версию")
                snapshot = self.checked_snapshot(
                    self.version(cur, program_id, body.version_id), review_id
                )
                source_documents = review["parameters"]["input"]["documents"]
                ruleset = review["report"]["result"]["ruleset"]
                if executor == "local":
                    # A pending review can outlive the image that prepared it. A new check
                    # must use the validator code and rules currently running in the worker,
                    # rather than inherit a revision that would make the check fail as stale.
                    from app.validation_bridge import (
                        validation_documents,
                        validation_implementation_revision,
                        validation_ruleset,
                    )
                    from worker.validation.validator import load_rules

                    local_rules = load_rules()
                    ruleset = validation_ruleset(
                        local_rules, validation_documents(source_documents)
                    )
                    parameters["implementation_revision"] = (
                        validation_implementation_revision()
                    )
                    parameters["local_rules"] = local_rules
                else:
                    from app.validation_bridge import required_validation_rules

                    ruleset = {
                        **ruleset,
                        "required_rules": required_validation_rules(
                            ruleset.get("config", {}), "graph_validation"
                        ),
                    }
                rule_codes = getattr(body, "rule_codes", None)
                document_ids = getattr(body, "document_ids", None)
                if rule_codes is not None:
                    rule_definitions = ruleset.get("config", {}).get("rules", [])
                    manual_rule_codes = {
                        rule.get("id")
                        for rule in rule_definitions
                        if rule.get("status") in {"ready", "ready_with_semantic_fallback"}
                        and rule.get("run_mode", "automatic") == "manual"
                        and rule.get("phases") == ["graph_validation"]
                    }
                    invalid_codes = set(rule_codes) - manual_rule_codes
                    if invalid_codes:
                        raise DomainError(
                            422,
                            "Нельзя запустить вручную правила: "
                            + ", ".join(sorted(invalid_codes)),
                        )
                    parameters["manual_rule_codes"] = list(rule_codes)
                    ruleset = {**ruleset, "required_rules": []}
                if document_ids is not None:
                    if rule_codes is None or "R-11" not in rule_codes:
                        raise DomainError(
                            422,
                            "Выбор документа допустим только для ручной проверки R-11",
                        )
                    known = {
                        str(item["document_id"]): (item.get("parsed") or {}).get("meta", {}).get("doc_type")
                        for item in source_documents
                    }
                    unknown = [str(value) for value in document_ids if str(value) not in known]
                    not_rpd = [
                        str(value) for value in document_ids
                        if str(value) in known and known[str(value)] != "RPD"
                    ]
                    if unknown or not_rpd:
                        raise DomainError(
                            422,
                            "Проверка по документу доступна только для РПД этого пакета",
                        )
                    parameters["manual_document_ids"] = [str(value) for value in document_ids]
                parameters.update(
                    content_hash=content_hash(snapshot),
                    ruleset=ruleset,
                    project_check_documents=(review["parameters"].get("schema_version") == 2),
                    input={
                        "snapshot": snapshot.model_dump(mode="json"),
                        "ruleset": ruleset,
                        **({"rule_codes": list(rule_codes)} if rule_codes is not None else {}),
                        **({"document_ids": [str(value) for value in document_ids]}
                           if document_ids is not None else {}),
                        # Worker повторно накладывает сохранённые решения после чтения
                        # исходных parsed-артефактов; изменённый JSON не подменяет оригинал.
                        "documents": source_documents,
                    },
                )
                version_id = body.version_id
            job_id = uuid4()
            size = len(json.dumps(parameters, ensure_ascii=False, allow_nan=False).encode())
            # Local checks are stored as packed rows and Temporal carries only the
            # job ID. The JSON request limit applies only to the external analyzer.
            if executor != "local" and size > self.settings.analysis_max_request_bytes:
                raise DomainError(413, "Вход анализа превышает настроенный предел JSON")
            if purpose == "prepare":
                parameters["review_id"] = str(job_id)
            cur.execute(
                "INSERT INTO processing_jobs(id,program_id,input_version_id,"
                "input_graph_state_id,operation_key,\n                   request_hash,"
                "type,temporal_namespace,workflow_id,parameters)\n                   "
                "VALUES(%s,%s,%s,(SELECT graph_state_id FROM program_versions WHERE "
                "id=%s),%s,%s,'validate',%s,%s,%s)",
                (
                    job_id,
                    program_id,
                    version_id,
                    version_id,
                    key,
                    fingerprint,
                    self.settings.temporal_namespace,
                    f"review-{job_id}",
                    Jsonb(pack_tree(cur, program_id, parameters)),
                ),
            )
            return job_id

    def job(self, program_id, job_id):
        with self.pool.connection() as connection, connection.cursor(row_factory=dict_row) as cur:
            return hydrate_job(
                cur,
                Repository.require(
                    cur.execute(
                        """SELECT * FROM processing_jobs WHERE program_id=%s AND id=%s
                   AND type='validate' AND parameters->>'purpose' IN ('prepare','check')""",
                        (program_id, job_id),
                    ).fetchone()
                ),
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
        from app.audit_storage import save_response

        raw_artifact = save_response(raw_result)
        job = self.job_by_id(job_id)
        program_id, purpose = job["program_id"], job["parameters"]["purpose"]
        with self.pool.connection() as connection, connection.cursor(row_factory=dict_row) as cur:
            current = self.lock(cur, program_id)
            locked = cur.execute(
                "SELECT * FROM processing_jobs WHERE id=%s FOR UPDATE", (job_id,)
            ).fetchone()
            if locked["workflow_id"] != job["workflow_id"] or "outcome" in locked["report"]:
                return locked["report"].get("outcome", "failed")
            if purpose == "prepare":
                for artifact in artifacts:
                    cur.execute(
                        "INSERT INTO document_artifacts(\n                           id,"
                        "program_id,document_id,producing_job_id,producing_attempt,kind,"
                        "\n                           parser_revision,parser_config,"
                        "parser_config_hash,hash_encoding,storage_key,sha256,"
                        "size_bytes)\n                           VALUES(%s,%s,%s,%s,1,"
                        "'markdown',%s,%s,%s,'raw-bytes',%s,%s,%s)",
                        (
                            UUID(artifact["id"]),
                            program_id,
                            UUID(artifact["document_id"]),
                            job_id,
                            artifact["parser_revision"],
                            Jsonb(artifact["parser_config"]),
                            artifact["parser_config_hash"],
                            artifact["id"],
                            artifact["sha256"],
                            artifact["size_bytes"],
                        ),
                    )
            if purpose == "prepare" and current != job["input_version_id"]:
                candidate = None
                if job["parameters"].get("schema_version", 1) == 1:
                    from app.graph_storage import save_graph

                    candidate = save_graph(
                        cur, program_id, result["candidate"], producer=job_id, attempt=1
                    )
                    result = {
                        **result,
                        "candidate": {
                            "__graph_state": str(candidate),
                            "review": result["candidate"].get("review"),
                        },
                    }
                report = {
                    "outcome": "conflict",
                    "result": result,
                    "raw_result_artifact": raw_artifact,
                    "raw_result_hash": raw_hash,
                    "message": "Версия изменилась во время анализа; результат сохранён",
                }
                cur.execute(
                    "UPDATE processing_jobs SET report=%s,output_graph_state_id=%s WHERE id=%s",
                    (Jsonb(pack_tree(cur, program_id, report)), candidate, job_id),
                )
                return "conflict"
            output_id = None
            if purpose == "prepare":
                output_id = uuid5(job_id, "analysis-version")
                if job["parameters"].get("schema_version", 1) == 2:
                    decisions_pending = any(item["requires_decision"] for item in result["issues"])
                    build_errors = []
                    original_documents = {
                        item["document_id"]: item["parsed"]
                        for item in job["parameters"]["input"]["documents"]
                    }
                    if decisions_pending:
                        graph_data = {
                            "schema_version": 2,
                            "metadata": {},
                            "document_ids": [],
                            "entities": [],
                            "relations": [],
                            "source_refs": [],
                            "source_links": [],
                        }
                    else:
                        from app.document_adapter import to_snapshot_v2

                        files = [
                            {"document_id": identity, "output": document}
                            for identity, document in original_documents.items()
                        ]
                        graph_data, build_issues = to_snapshot_v2(
                            program_id, files, original_files=files
                        )
                        build_errors = graph_build_issues(build_issues)
                        if build_errors:
                            graph_data = {
                                "schema_version": 2,
                                "metadata": {},
                                "document_ids": [],
                                "entities": [],
                                "relations": [],
                                "source_refs": [],
                                "source_links": [],
                            }
                        else:
                            attach_document_artifacts(graph_data, result.get("artifacts", []))
                    state = SnapshotV2.model_validate(graph_data).model_dump(mode="json")
                    state["review"] = {
                        "review_id": str(job_id),
                        "import_job_id": job["parameters"]["import_job_id"],
                        "analysis_version_id": str(output_id),
                        "graph_ready": not decisions_pending and not build_errors,
                        "decisions": [],
                        "confirmation": None,
                    }
                    snapshot = SnapshotV2.model_validate(state)
                    result["graph_ready"] = not decisions_pending and not build_errors
                    result["graph_build_issues"] = build_errors
                else:
                    snapshot = SnapshotV2.model_validate(result["candidate"])
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
            cur.execute(
                "UPDATE processing_jobs SET output_version_id=%s,output_graph_state_id="
                "(SELECT graph_state_id FROM program_versions WHERE "
                "id=%s),checked_version_id=%s WHERE id=%s",
                (output_id, output_id, issue_version, job_id),
            )
            insert_issues(cur, program_id, job_id, issue_version, result["issues"])
            report = {
                "outcome": "completed",
                "result": result,
                "raw_result_artifact": raw_artifact,
                "raw_result_hash": raw_hash,
                "completed_at": datetime.now(UTC).isoformat(),
            }
            cur.execute(
                "UPDATE processing_jobs SET output_version_id=%s,report=%s WHERE id=%s",
                (output_id, Jsonb(pack_tree(cur, program_id, report)), job_id),
            )
            return "completed"

    def fail(self, job_id, workflow_id, code, message):
        with self.pool.connection() as connection:
            connection.execute(
                "SELECT p.id FROM programs p JOIN processing_jobs j ON "
                "j.program_id=p.id "
                "WHERE j.id=%s FOR UPDATE OF p",
                (job_id,),
            )
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
            snapshot = self.checked_snapshot(
                self.version(cur, program_id, current), review_id, require_graph=False
            )
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
            if review["parameters"].get("schema_version") == 2:
                if issue["job_id"] != review_id:
                    raise DomainError(409, "Предграфовое решение должно относиться к AnalysisV2")
                if any(
                    change.get("op") not in {"set_document_value", "set_competency_links"}
                    for decision in snapshot.review.decisions for change in decision.changes
                ):
                    raise DomainError(
                        409, "После правок графа нельзя пересобирать его предграфовым выбором"
                    )
                documents = review["parameters"]["input"]["documents"]
                original = {item["document_id"]: item["parsed"] for item in documents}
                choices = [
                    item.model_dump(mode="json")
                    for item in snapshot.review.decisions
                    if item.conflict_id != body.conflict_id
                ]
                changes = [change for choice in choices for change in choice["changes"]]
                changes.extend(variant["changes"])
                relation_origins = {}
                selected_documents = apply_document_mutations(
                    original, changes, relation_origins=relation_origins
                )
                choices.append(
                    {
                        "conflict_id": str(body.conflict_id),
                        "variant_id": str(body.variant_id),
                        "check_job_id": str(review_id),
                        "changes": variant["changes"],
                        "decided_at": datetime.now(UTC).isoformat(),
                        "author": body.signature,
                    }
                )
                required = {
                    item["id"]
                    for item in review["report"]["result"]["issues"]
                    if item["requires_decision"]
                }
                decided = {str(item["conflict_id"]) for item in choices}
                graph_ready = required.issubset(decided)
                build_errors = []
                if graph_ready:
                    from app.document_adapter import to_snapshot_v2

                    files = [
                        {"document_id": identity, "output": value}
                        for identity, value in selected_documents.items()
                    ]
                    graph_data, build_issues = to_snapshot_v2(
                        program_id, files, original_files=documents,
                        relation_origins=relation_origins,
                    )
                    build_errors = graph_build_issues(build_issues)
                    if build_errors:
                        graph_ready = False
                        graph_data = snapshot
                    else:
                        attach_document_artifacts(
                            graph_data, review["report"]["result"].get("artifacts", [])
                        )
                else:
                    graph_data = snapshot
                state = (
                    graph_data.model_dump(mode="json")
                    if isinstance(graph_data, SnapshotV2)
                    else SnapshotV2.model_validate(graph_data).model_dump(mode="json")
                )
                if state["review"] is None:
                    state["review"] = snapshot.review.model_dump(mode="json")
                state["review"]["graph_ready"] = graph_ready
                state["review"]["decisions"] = choices
                next_snapshot = SnapshotV2.model_validate(state)
                version = insert_version(
                    cur,
                    program_id,
                    current,
                    uuid4(),
                    next_snapshot,
                    key=key,
                    fingerprint=fingerprint,
                    actor_type="user",
                    actor_id=body.signature,
                )
                review_result = review["report"].get("result", {})
                review_result["graph_ready"] = graph_ready
                review_result["graph_build_issues"] = build_errors
                cur.execute(
                    "UPDATE processing_jobs SET report=%s WHERE id=%s",
                    (Jsonb(pack_tree(cur, program_id, review["report"])), review_id),
                )
                return version
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
                    "author": body.signature,
                }
            )
            selected = apply_changes(
                base, [change for choice in choices for change in choice["changes"]]
            )
            state = selected.model_dump(mode="json")
            state["review"]["decisions"] = choices
            selected = SnapshotV2.model_validate(state)
            version_id = uuid4()
            return insert_version(
                cur,
                program_id,
                current,
                version_id,
                selected,
                key=key,
                fingerprint=fingerprint,
                actor_type="user",
                actor_id=body.signature,
            )

    @staticmethod
    def job_with_cursor(cur, program_id, job_id):
        return hydrate_job(
            cur,
            Repository.require(
                cur.execute(
                    "SELECT * FROM processing_jobs WHERE program_id=%s AND id=%s AND "
                    "type='validate'",
                    (program_id, job_id),
                ).fetchone()
            ),
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
                or checked["parameters"].get("manual_rule_codes") is not None
                or checked["input_version_id"] != current
                or checked["report"].get("outcome") != "completed"
                or checked["parameters"].get("content_hash") != content_hash(snapshot)
            ):
                raise DomainError(409, "Нужна завершённая проверка точной текущей версии")
            result = CheckResult.model_validate(
                project_legacy_review_result(checked["report"]["result"])
            )
            if digest(result.ruleset.model_dump(mode="json")) != digest(
                checked["parameters"]["ruleset"]
            ):
                raise DomainError(409, "Набор правил проверки изменился")
            coverage = {item.rule_code: item.status for item in result.coverage}
            if any(
                coverage.get(code) not in {"passed", "failed"}
                for code in result.ruleset.required_rules
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
                "author": body.signature,
            }
            snapshot = SnapshotV2.model_validate(state)
            version_id = uuid4()
            if content_hash(snapshot) != hash_before:
                raise DomainError(409, "Содержимое не совпадает с проверенным")
            return insert_version(
                cur,
                program_id,
                current,
                version_id,
                snapshot,
                key=key,
                fingerprint=fingerprint,
                actor_type="user",
                actor_id=body.signature,
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
                       AND parameters->>'manual_rule_codes' IS NULL
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
        issues = []
        for item in stored:
            details = item["details"]
            decision = choices.get(str(details["id"]))
            issues.append({
                **project_legacy_review_issue(
                    details,
                    selected_variant_id=decision.get("variant_id") if decision else None,
                ),
                "source_version_id": item["version_id"],
                "job_id": item["job_id"],
            })
        if latest:
            # The completed graph check re-evaluates R-08 against the current version.
            # Keep unresolved prepare-time R-08 decisions; these findings do not create
            # document mutations by themselves.
            decided_ids = set(choices)
            issues = [
                item
                for item in issues
                if not (
                    str(item["job_id"]) == str(review["id"])
                    and str(item["rule_code"]).startswith("R-08")
                    and (
                        not item.get("requires_decision")
                        or str(item["id"]) in decided_ids
                    )
                )
            ]
        build_errors = review["report"].get("result", {}).get("graph_build_issues", [])
        if snapshot.review.graph_ready is False:
            for item in build_errors:
                issue_id = uuid5(
                    snapshot.review.review_id,
                    "graph-build:" + digest(item),
                )
                issues.append(
                    {
                        "id": str(issue_id),
                        "rule_code": "BACKEND_GRAPH_BUILD",
                        "rule_revision": digest({"builder": "document-adapter-v2"}),
                        "type": item["category"],
                        "severity": "blocking",
                        "status": "open",
                        "message": item["message"],
                        "source_ref_ids": [],
                        "source_document_ids": [item["document_id"]],
                        "requires_decision": False,
                        "variants": [],
                        "source_version_id": version_id,
                        "job_id": snapshot.review.review_id,
                    }
                )
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
            "check_report": (
                project_legacy_review_result(latest["report"]["result"])
                if latest
                else None
            ),
            "prepare_report": project_prepare_report(review["report"]["result"]),
        }
