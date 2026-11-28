"""Бизнес-задачи анализа и неизменяемые версии выбранных исправлений."""

import json
from contextlib import nullcontext
from datetime import UTC, datetime
from hashlib import sha256
from uuid import UUID, uuid4, uuid5

from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from app.config import get_settings
from app.domain import ReviewDecision, ReviewState, SnapshotV2
from app.errors import DomainError
from app.graph_storage import canonical_bytes, hydrate_job, pack_tree, unpack_tree
from app.repository import Repository, limit_lock_wait
from app.review_contract import DOCUMENT_MUTATION_OPS, CheckCreate, CheckResult, DecisionBatchCreate
from app.review_logic import (
    custom_decision_changes,
    digest,
)
from app.review_projection import (
    project_legacy_review_issue,
    project_legacy_review_result,
    project_prepare_report,
    visible_prepare_findings,
)
from app.review_storage import insert_issues, insert_version, previous_version


def manual_check_targets(snapshot, documents):
    """Use the current graph's catalogue codes and the pinned package's RPDs."""
    members = set(map(str, snapshot.document_ids))
    metadata = {
        str(item["document_id"]): item["parsed"].get("meta", {})
        for item in documents if str(item["document_id"]) in members
    }
    catalogues = {
        identity for identity, meta in metadata.items()
        if meta.get("doc_type") in {"CKMV", "CHARACTERISTIC"}
    }
    catalogue_refs = {
        ref.id for ref in snapshot.source_refs if str(ref.document_id) in catalogues
    }
    competencies = {}
    for entity in snapshot.entities:
        if entity.kind != "competency" or catalogue_refs.isdisjoint(entity.source_ref_ids):
            continue
        code, formulation = entity.attributes.get("code"), entity.attributes.get("formulation")
        if isinstance(code, str) and code.strip():
            code = code.strip()
            if not competencies.get(code):
                competencies[code] = formulation if isinstance(formulation, str) else ""
    return {
        "rpd": [
            {"document_id": identity, "name": meta.get("file_name") or identity}
            for identity, meta in metadata.items() if meta.get("doc_type") == "RPD"
        ],
        "competencies": [
            {"code": code, "formulation": formulation}
            for code, formulation in sorted(competencies.items())
        ],
    }


def compact_manual_check_targets(cur, graph_state_id):
    """Получить цели ручных проверок из разборов и ревизий без сборки всего графа."""
    if graph_state_id is None:
        return {"rpd": [], "competencies": []}
    documents = cur.execute(
        """SELECT d.document_id,a.parsed_output->'meta' AS meta
           FROM graph_documents d JOIN document_artifacts a ON a.id=d.parsed_artifact_id
           WHERE d.graph_state_id=%s ORDER BY d.position""",
        (graph_state_id,),
    ).fetchall()
    metadata = {
        str(item["document_id"]): item["meta"] or {}
        for item in documents
    }
    catalogues = [
        identity for identity, meta in metadata.items()
        if meta.get("doc_type") in {"CKMV", "CHARACTERISTIC"}
    ]
    competencies = []
    if catalogues:
        rows = cur.execute(
            """SELECT e.attributes->>'code' AS code,
                      e.attributes->>'formulation' AS formulation
               FROM graph_entities g JOIN entity_revisions e ON e.revision_id=g.revision_id
               WHERE g.graph_state_id=%s AND e.kind='competency'
                 AND EXISTS (
                   SELECT 1 FROM revision_sources rs JOIN source_refs r
                     ON r.id=rs.source_ref_id
                   WHERE rs.entity_revision_id=e.revision_id
                     AND r.document_id=ANY(%s::uuid[])
                 )
               ORDER BY e.attributes->>'code',g.position""",
            (graph_state_id, catalogues),
        ).fetchall()
        by_code = {}
        for item in rows:
            code = item["code"].strip() if isinstance(item["code"], str) else ""
            if code and code not in by_code:
                by_code[code] = item["formulation"] or ""
        competencies = [
            {"code": code, "formulation": formulation}
            for code, formulation in sorted(by_code.items())
        ]
    return {
        "rpd": [
            {"document_id": identity, "name": meta.get("file_name") or identity}
            for identity, meta in metadata.items() if meta.get("doc_type") == "RPD"
        ],
        "competencies": competencies,
    }


def attach_document_artifacts(snapshot_data, artifacts):
    by_document = {str(item["document_id"]): str(item["id"]) for item in artifacts}
    if isinstance(snapshot_data, SnapshotV2):
        refs = [
            item.model_copy(update={"artifact_id": UUID(by_document[str(item.document_id)]),
                                    "artifact_locator": "document"})
            if str(item.document_id) in by_document else item
            for item in snapshot_data.source_refs
        ]
        return snapshot_data.model_copy(update={"source_refs": refs})
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


def pack_check_documents(cur, program_id, documents):
    """Заменить разборы ссылками одним поиском артефактов на весь пакет."""
    hashes = [
        sha256(canonical_bytes(item["parsed"])).hexdigest()
        if isinstance(item.get("parsed"), dict)
        and set(item["parsed"]) != {"__parsed_artifact"}
        else None
        for item in documents
    ]
    wanted = [
        (UUID(str(item["document_id"])), checksum)
        for item, checksum in zip(documents, hashes, strict=True)
        if checksum is not None
    ]
    if not wanted:
        return documents
    rows = cur.execute(
        """WITH requested AS (
               SELECT * FROM unnest(%s::uuid[],%s::text[]) AS x(document_id,sha256)
           )
           SELECT DISTINCT ON (r.document_id,r.sha256)
               r.document_id,r.sha256,a.id
           FROM requested r JOIN document_artifacts a
             ON a.program_id=%s AND a.document_id=r.document_id
             AND a.sha256=r.sha256 AND a.kind='parsed_json'
           ORDER BY r.document_id,r.sha256,a.created_at DESC,a.id""",
        ([item[0] for item in wanted], [item[1] for item in wanted], program_id),
    ).fetchall()
    artifacts = {
        (str(row["document_id"]), row["sha256"]): str(row["id"])
        for row in rows
    }
    packed = []
    for item, checksum in zip(documents, hashes, strict=True):
        if checksum is None:
            packed.append(item)
            continue
        artifact_id = artifacts.get((str(item["document_id"]), checksum))
        if artifact_id is None:
            raise DomainError(422, "Вход проверки не связан с сохранённым разбором")
        packed.append({**item, "parsed": {"__parsed_artifact": artifact_id}})
    return packed


class ReviewRepository:
    def __init__(self, pool):
        self.pool = pool
        self.settings = get_settings()

    @staticmethod
    def lock(cur, program_id):
        # 5 с — это ожидание занятой программы; сборка и запись графа после
        # захвата идёт с широким лимитом (см. repository.limit_lock_wait).
        limit_lock_wait(cur)
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
        return snapshot

    def enqueue_graph_build(self, cur, program_id, review_id, version_id):
        """Persist the final graph build request in the same transaction as the decision."""
        job_id = uuid4()
        workflow_id = f"graph-build-{job_id}"
        parameters = {
            "purpose": "build_graph",
            "review_id": str(review_id),
            "pending_version_id": str(version_id),
        }
        cur.execute(
            """INSERT INTO processing_jobs(id,program_id,input_version_id,operation_key,
               request_hash,type,temporal_namespace,workflow_id,parameters,report)
               VALUES(%s,%s,%s,%s,%s,'validate',%s,%s,%s,%s)""",
            (
                job_id,
                program_id,
                version_id,
                f"graph-build:{job_id}",
                digest(parameters),
                self.settings.temporal_namespace,
                workflow_id,
                Jsonb(parameters),
                Jsonb({"status": "pending", "message": "Собираем граф программы"}),
            ),
        )
        return job_id

    def graph_build_pending(self):
        with self.pool.connection() as connection:
            return connection.execute(
                """SELECT id,workflow_id FROM processing_jobs WHERE type='validate'
                   AND parameters->>'purpose'='build_graph' AND NOT report ? 'outcome'
                   ORDER BY created_at,id"""
            ).fetchall()

    def mark_graph_build_started(self, job_id):
        with self.pool.connection() as connection:
            connection.execute(
                """UPDATE processing_jobs SET report=report || %s
                   WHERE id=%s AND type='validate'
                   AND parameters->>'purpose'='build_graph' AND NOT report ? 'outcome'""",
                (Jsonb({"status": "processing", "message": "Собираем граф программы"}), job_id),
            )

    def graph_build_status(self, program_id, review_id):
        with self.pool.connection() as connection, connection.cursor(row_factory=dict_row) as cur:
            row = cur.execute(
                """SELECT id,input_version_id,output_version_id,report FROM processing_jobs
                   WHERE program_id=%s AND type='validate'
                   AND parameters->>'purpose'='build_graph'
                   AND parameters->>'review_id'=%s ORDER BY created_at DESC,id DESC LIMIT 1""",
                (program_id, str(review_id)),
            ).fetchone()
        if row is None:
            raise DomainError(404, "Сборка графа для этой сверки не найдена")
        report = row["report"]
        return {
            "id": row["id"],
            "pending_version_id": row["input_version_id"],
            "output_version_id": row["output_version_id"],
            "status": report.get("outcome") or report.get("status") or "pending",
            "message": report.get("message"),
        }

    def build_graph(self, job_id):
        """Build and persist the final graph outside the decision HTTP request."""
        with self.pool.connection() as connection, connection.cursor(row_factory=dict_row) as cur:
            job = Repository.require(
                cur.execute(
                    """SELECT * FROM processing_jobs WHERE id=%s AND type='validate'
                       AND parameters->>'purpose'='build_graph' FOR UPDATE""",
                    (job_id,),
                ).fetchone()
            )
            if job["report"].get("outcome") == "completed":
                return str(job["output_version_id"])

            program_id = job["program_id"]
            current = self.lock(cur, program_id)
            pending_version_id = UUID(job["parameters"]["pending_version_id"])
            review_id = UUID(job["parameters"]["review_id"])
            if current != pending_version_id:
                cur.execute(
                    "UPDATE processing_jobs SET report=%s WHERE id=%s",
                    (
                        Jsonb({"outcome": "conflict", "message": "Версия программы изменилась"}),
                        job_id,
                    ),
                )
                return "conflict"

            pending = self.checked_snapshot(
                self.version(cur, program_id, pending_version_id),
                review_id,
                require_graph=False,
            )
            review = self.require_review(cur, program_id, review_id)
            documents = review["parameters"]["input"]["documents"]
            from app.source_graph import apply_document_selections
            from app.version_policy import pending_document_choices
            graph = cur.execute(
                "SELECT graph_state_id FROM program_versions WHERE id=%s", (current,)
            ).fetchone()["graph_state_id"]
            ancestors = cur.execute(
                "SELECT review FROM program_versions WHERE program_id=%s AND graph_state_id=%s "
                "ORDER BY number DESC",
                (program_id, graph),
            ).fetchall()
            base_review = next(
                value
                for row in ancestors
                if (value := ReviewState.model_validate(row["review"]))
                and not pending_document_choices(value)
            )
            base = pending.model_copy(update={"review": base_review})
            snapshot = apply_document_selections(
                program_id,
                documents,
                base,
                pending_document_choices(pending.review),
            )
            snapshot = snapshot.model_copy(
                update={
                    "review": pending.review.model_copy(
                        update={
                            "projected_decision_ids": [
                                item.conflict_id for item in pending.review.decisions
                            ],
                            "confirmation": None,
                        }
                    )
                }
            )
            version = insert_version(
                cur,
                program_id,
                current,
                uuid4(),
                snapshot,
                actor_type="system",
                actor_id="graph-builder",
                before_snapshot=pending,
            )
            self.create(
                program_id,
                "automatic-check:" + str(version["id"]),
                CheckCreate(version_id=version["id"]),
                purpose="check",
                review_id=review_id,
                connection=connection,
                snapshot_override=snapshot,
                review_override=review,
            )
            graph = cur.execute(
                "SELECT graph_state_id FROM program_versions WHERE id=%s", (version["id"],)
            ).fetchone()["graph_state_id"]
            cur.execute(
                "UPDATE processing_jobs SET "
                "output_version_id=%s,output_graph_state_id=%s,report=%s WHERE id=%s",
                (
                    version["id"],
                    graph,
                    Jsonb({"outcome": "completed", "message": "Изменения применены"}),
                    job_id,
                ),
            )
            return str(version["id"])

    def fail_graph_build(self, job_id, message):
        with self.pool.connection() as connection:
            connection.execute(
                """UPDATE processing_jobs SET report=%s WHERE id=%s AND type='validate'
                   AND parameters->>'purpose'='build_graph' AND NOT report ? 'outcome'""",
                (Jsonb({"outcome": "failed", "message": message}), job_id),
            )

    def create(
        self, program_id, key, body, *, purpose="prepare", review_id=None,
        allow_historical=False,
        connection=None,
        snapshot_override=None,
        review_override=None,
    ):
        request = body.model_dump(mode="json", exclude_none=True)
        fingerprint = digest({"operation": purpose, "review_id": str(review_id), "body": request})
        with (
            nullcontext(connection) if connection is not None else self.pool.connection()
        ) as connection, connection.cursor(row_factory=dict_row) as cur:
            current = self.lock(cur, program_id)
            previous = cur.execute(
                """SELECT id,request_hash,type,parameters->'retry_requests' AS retry_requests
                   FROM processing_jobs WHERE program_id=%s
                   AND (operation_key=%s OR parameters->'request_keys' ? %s)""",
                (program_id, key, key),
            ).fetchone()
            if previous:
                retry_hash = (previous["retry_requests"] or {}).get(key)
                if previous["type"] != "validate" or (
                    previous["request_hash"] != fingerprint and retry_hash != fingerprint
                ):
                    raise DomainError(409, "Ключ использован для другого запроса")
                return previous["id"]
            if cur.execute(
                "SELECT id FROM program_versions WHERE program_id=%s AND operation_key=%s",
                (program_id, key),
            ).fetchone():
                raise DomainError(409, "Ключ использован для сохранения версии")
            if (purpose == "prepare"
                and not body.rerun_same_import):
                duplicate = cur.execute(
                    "SELECT id,request_hash FROM processing_jobs WHERE program_id=%s "
                    "AND input_version_id=%s AND parameters->>'purpose'='prepare' "
                    "AND parameters->>'import_job_id'=%s "
                    "AND parameters->>'lifecycle_policy'='automatic_source_v2' "
                    "ORDER BY created_at,id LIMIT 1",
                    (program_id, body.expected_version_id, str(body.import_job_id)),
                ).fetchone()
                if duplicate and duplicate["request_hash"] == fingerprint:
                    cur.execute(
                        "UPDATE processing_jobs SET parameters=jsonb_set(parameters,"
                        "'{request_keys}',COALESCE(parameters->'request_keys','[]'::jsonb) "
                        "|| to_jsonb(%s::text)) WHERE id=%s",
                        (key, duplicate["id"]),
                    )
                    return duplicate["id"]
            if (
                not self.settings.analysis_url
                and not self.settings.local_validation_enabled
                and not (purpose == "prepare")
            ):
                raise DomainError(503, "ML-анализ не настроен; фиктивный результат не создаётся")
            executor = "http" if self.settings.analysis_url else "local"
            parameters = {
                "purpose": purpose,
                "review_id": str(review_id) if review_id else None,
                "executor": executor,
            }
            if purpose == "prepare":
                parameters["lifecycle_policy"] = "automatic_source_v2"
                if current != body.expected_version_id:
                    raise DomainError(409, "Текущая версия изменилась")
                current_snapshot = self.version(cur, program_id, current)["snapshot"]
                current_review = current_snapshot.get("review") or {}
                if (current_review.get("review_id")
                    and str(current_review.get("import_job_id")) == str(body.import_job_id)):
                    # Повторяем только анализ; сохранённые версии и правки не пересоздаём.
                    old = self.require_review(cur, program_id, UUID(current_review["review_id"]))
                    retry_parameters = {
                        **old["parameters"],
                        "request_keys": [*old["parameters"].get("request_keys", []), key],
                        "retry_requests": {
                            **old["parameters"].get("retry_requests", {}), key: fingerprint,
                        },
                    }
                    retry_report = old["report"]
                    workflow_id = old["workflow_id"]
                    if retry_report.get("outcome") == "failed" and body.rerun_same_import:
                        retry_report = {k: v for k, v in retry_report.items()
                                        if k not in {"outcome", "code", "message"}}
                        workflow_id = f"review-{old['id']}-retry-{uuid4()}"
                        if retry_parameters.get("executor") == "local":
                            from app.validation_bridge import validation_implementation_revision
                            from worker.validation.validator import load_rules

                            retry_parameters["local_rules"] = load_rules()
                            retry_parameters["implementation_revision"] = (
                                validation_implementation_revision()
                            )
                    cur.execute(
                        "UPDATE processing_jobs SET parameters=%s,report=%s,workflow_id=%s "
                        "WHERE id=%s",
                        (Jsonb(pack_tree(cur, program_id, retry_parameters)),
                         Jsonb(pack_tree(cur, program_id, retry_report)), workflow_id, old["id"]),
                    )
                    return old["id"]
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
                            for item in previous_review["report"]
                            .get("result", {})
                            .get("issues", [])
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
                review = review_override or self.require_review(cur, program_id, review_id)
                if (
                    str(review["id"]) != str(review_id)
                    or str(review["program_id"]) != str(program_id)
                ):
                    raise DomainError(404, "Проверка из другой подготовки")
                executor = review["parameters"].get("executor", "http")
                parameters["executor"] = executor
                if body.version_id != current and not allow_historical:
                    raise DomainError(409, "Проверять нужно текущую версию")
                stored_version = Repository.require(
                    cur.execute(
                        "SELECT v.graph_state_id,v.review,s.content_hash FROM program_versions v "
                        "LEFT JOIN graph_states s ON s.id=v.graph_state_id AND s.status='sealed' "
                        "WHERE v.program_id=%s AND v.id=%s",
                        (program_id, body.version_id),
                    ).fetchone()
                )
                if snapshot_override is None:
                    # Результаты ручных семантических проверок привязаны к задаче
                    # и не меняют сохранённую версию.
                    snapshot = self.checked_snapshot(
                        self.version(cur, program_id, body.version_id), review_id,
                    )
                else:
                    snapshot = snapshot_override
                    if (
                        snapshot.review is None
                        or snapshot.review.review_id != review_id
                    ):
                        raise DomainError(404, "Версия не принадлежит этой подготовке")
                    if not snapshot.review.graph_ready:
                        raise DomainError(
                            409, "Граф ещё не собран из выбранных значений документов"
                        )
                parameters["lifecycle_policy"] = snapshot.review.lifecycle_policy
                if stored_version["graph_state_id"] is None:
                    raise DomainError(409, "У проверяемой версии нет сохранённого графа")
                if stored_version["content_hash"] is None:
                    raise DomainError(409, "Проверка требует запечатанного графа с хэшем")
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
                competency_code = getattr(body, "competency_code", None)
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
                    targets = manual_check_targets(snapshot, source_documents)
                    valid_rpd = {item["document_id"] for item in targets["rpd"]}
                    invalid = [str(value) for value in document_ids if str(value) not in valid_rpd]
                    if invalid:
                        raise DomainError(422, "Для R-11 выберите РПД из этого пакета")
                    parameters["manual_document_ids"] = [str(value) for value in document_ids]
                if competency_code is not None:
                    if rule_codes is None or "R-09" not in rule_codes:
                        raise DomainError(
                            422,
                            "Выбор компетенции допустим только для ручной проверки R-09",
                        )
                    targets = manual_check_targets(snapshot, source_documents)
                    valid_codes = {item["code"] for item in targets["competencies"]}
                    if competency_code not in valid_codes:
                        raise DomainError(
                            422, "Для R-09 выберите компетенцию из характеристики или ЦКМВ"
                        )
                    parameters["manual_competency_code"] = competency_code
                # Снимок уже запечатан в версии. Передаём ссылку на состояние,
                # чтобы pack_tree не собирал весь граф повторно в той же транзакции.
                snapshot_reference = {
                    "__graph_state": str(stored_version["graph_state_id"]),
                    "review": stored_version["review"],
                }
                parameters.update(
                    # Хэш вычислен при записи и защищён вместе с sealed-состоянием.
                    # Review не входит в него; выгрузка отдельно сверяет содержимое.
                    content_hash=stored_version["content_hash"],
                    ruleset=ruleset,
                    project_check_documents=(review["parameters"].get("schema_version") == 2),
                    input={
                        "snapshot": snapshot_reference,
                        "ruleset": ruleset,
                        **({"rule_codes": list(rule_codes)} if rule_codes is not None else {}),
                        **({"document_ids": [str(value) for value in document_ids]}
                           if document_ids is not None else {}),
                        **(
                            {"competency_code": competency_code}
                            if competency_code is not None
                            else {}
                        ),
                        # Worker повторно накладывает сохранённые решения после чтения
                        # исходных parsed-артефактов; изменённый JSON не подменяет оригинал.
                        "documents": source_documents,
                    },
                )
                version_id = body.version_id
            job_id = uuid4()
            # Local checks are stored as packed rows and Temporal carries only the
            # job ID. The JSON request limit applies only to the external analyzer.
            if executor != "local":
                size = len(json.dumps(parameters, ensure_ascii=False, allow_nan=False).encode())
                if size > self.settings.analysis_max_request_bytes:
                    raise DomainError(413, "Вход анализа превышает настроенный предел JSON")
            if purpose == "check":
                parameters["input"]["documents"] = pack_check_documents(
                    cur, program_id, parameters["input"]["documents"]
                )
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

    def compact_job(self, program_id, job_id, *, include_summary=True):
        """Не извлекать разобранные документы и исходный граф для списка замечаний."""
        with self.pool.connection() as connection, connection.cursor(row_factory=dict_row) as cur:
            result = (
                "COALESCE(report->'result','{}'::jsonb) - 'candidate' - 'artifacts'"
                if include_summary else
                "jsonb_build_object('graph_build_issues',report->'result'->'graph_build_issues')"
            )
            row = Repository.require(
                cur.execute(
                    f"""SELECT id,
                               jsonb_build_object(
                                 'outcome',report->'outcome',
                                 'message',report->'message','result',{result}
                               ) AS report
                        FROM processing_jobs WHERE program_id=%s AND id=%s AND type='validate'
                          AND parameters->>'purpose'='prepare'""",
                    (program_id, job_id),
                ).fetchone()
            )
            row["report"] = unpack_tree(cur, row["report"])
            return row

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

    def note_progress(self, job_id, percent, stage):
        """Записать контрольную точку выполнения в отчёт задания.

        Workflow сверки знает только старт и финал, поэтому полоска
        прогресса в интерфейсе читает эти точки из отчёта, пока
        задание выполняется.
        """
        with self.pool.connection() as connection, connection.cursor() as cur:
            cur.execute(
                """UPDATE processing_jobs
                   SET report = report || jsonb_build_object(
                       'progress_percent', %s::int, 'progress_stage', %s::text)
                   WHERE id = %s AND NOT (report ? 'outcome')""",
                (int(percent), stage, job_id),
            )

    def publish(self, job_id, result, artifacts=(), raw_result=None, raw_hash=None,
                *, expected_workflow_id=None):
        from app.audit_storage import save_response

        raw_artifact = save_response(raw_result)
        job = self.job_by_id(job_id)
        program_id, purpose = job["program_id"], job["parameters"]["purpose"]
        automatic = (
            purpose == "prepare"
            and job["parameters"].get("schema_version", 1) == 2
        )
        if automatic:
            self.ensure_source_graph(job_id)
            job = self.job_by_id(job_id)
        with self.pool.connection() as connection, connection.cursor(row_factory=dict_row) as cur:
            current = self.lock(cur, program_id)
            locked = cur.execute(
                "SELECT * FROM processing_jobs WHERE id=%s FOR UPDATE", (job_id,)
            ).fetchone()
            if (locked["workflow_id"] != (expected_workflow_id or job["workflow_id"])
                or "outcome" in locked["report"]):
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
            if purpose == "prepare" and not automatic and current != job["input_version_id"]:
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
                    from app.source_graph import unresolved_findings

                    snapshot = SnapshotV2.model_validate(
                        self.version(cur, program_id, output_id)["snapshot"]
                    )
                    extra_coverage, extra_issues = unresolved_findings(snapshot, job_id)
                    result["coverage"].extend(extra_coverage)
                    result["issues"].extend(extra_issues)
                    result["graph_ready"] = True
                    result["graph_build_issues"] = []
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
                if not automatic:
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
            issues_to_insert = result["issues"]
            if automatic:
                existing = {
                    str(row["id"]) for row in cur.execute(
                        "SELECT id FROM validation_issues WHERE program_id=%s AND job_id=%s",
                        (program_id, job_id),
                    ).fetchall()
                }
                issues_to_insert = [item for item in issues_to_insert if item["id"] not in existing]
            insert_issues(cur, program_id, job_id, issue_version, issues_to_insert)
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
            if automatic:
                self.create(
                    program_id, "automatic-check:" + str(output_id),
                    CheckCreate(version_id=output_id), purpose="check", review_id=job_id,
                    allow_historical=True, connection=connection, snapshot_override=snapshot,
                    review_override={**job, "report": report},
                )
            return "completed"

    def ensure_source_graph(self, job_id):
        """Сохранить исходный граф до внешнего анализа и любых пользовательских правок."""
        from app.source_graph import source_snapshot, unresolved_findings

        job = self.job_by_id(job_id)
        if (
            job["parameters"].get("purpose") != "prepare"
            or job["parameters"].get("schema_version", 1) != 2
        ):
            return None
        if job["output_version_id"]:
            return job["output_version_id"]
        version_id = uuid5(job_id, "analysis-version")
        snapshot, _ = source_snapshot(
            job["program_id"], job["parameters"]["input"]["documents"],
            review_id=job_id, import_job_id=job["parameters"]["import_job_id"],
            version_id=version_id,
        )
        coverage, issues = unresolved_findings(snapshot, job_id)
        documents = job["parameters"]["input"]["documents"]
        scaffold = {
            "schema_version": 2,
            "ruleset": {
                "id": "source-graph-preparation", "revision": "source-graph-v1", "config": {},
                "required_rules": [],
                "required_document_types": sorted({
                    item["parsed"]["meta"]["doc_type"] for item in documents
                }),
            },
            "coverage": coverage, "issues": issues, "artifacts": [],
            "graph_ready": True, "graph_build_issues": [],
        }
        with self.pool.connection() as connection, connection.cursor(row_factory=dict_row) as cur:
            current = self.lock(cur, job["program_id"])
            locked = cur.execute(
                "SELECT output_version_id FROM processing_jobs WHERE id=%s FOR UPDATE", (job_id,),
            ).fetchone()
            if locked["output_version_id"]:
                return locked["output_version_id"]
            if current != job["input_version_id"]:
                raise DomainError(409, "Исходная версия изменилась до сохранения пакета")
            insert_version(cur, job["program_id"], current, version_id, snapshot)
            report = {
                **job["report"], "result": scaffold,
                "message": "Исходный граф сохранён; анализ документов ещё не завершён",
            }
            cur.execute(
                "UPDATE processing_jobs SET output_version_id=%s,output_graph_state_id="
                "(SELECT graph_state_id FROM program_versions WHERE id=%s),report=%s WHERE id=%s",
                (version_id, version_id, Jsonb(pack_tree(cur, job["program_id"], report)), job_id),
            )
            insert_issues(cur, job["program_id"], job_id, version_id, issues)
            return version_id

    def fail(self, job_id, workflow_id, code, message):
        with self.pool.connection() as connection:
            connection.execute(
                "SELECT p.id FROM programs p JOIN processing_jobs j ON "
                "j.program_id=p.id "
                "WHERE j.id=%s FOR UPDATE OF p",
                (job_id,),
            )
            connection.execute(
                """UPDATE processing_jobs SET report=(
                   report) || %s
                   WHERE id=%s AND workflow_id=%s
                   AND NOT report ? 'outcome'""",
                (
                    Jsonb({"outcome": "failed", "code": code, "message": message}),
                    job_id,
                    workflow_id,
                ),
            )


    def _save_document_choices(self, program_id, review_id, key, body, fingerprint):
        """Быстро сохранить решения, переиспользуя неизменённый исходный граф."""
        from app.graph_storage import journal

        selections = body.selections if isinstance(body, DecisionBatchCreate) else [body]
        with self.pool.connection() as conn, conn.cursor(row_factory=dict_row) as cur:
            current = self.lock(cur, program_id)
            previous = previous_version(cur, program_id, key, fingerprint)
            if previous:
                return previous
            if current != body.expected_version_id:
                raise DomainError(409, "Текущая версия изменилась")
            parent = cur.execute(
                "SELECT number,graph_state_id,review FROM program_versions WHERE "
                "program_id=%s AND id=%s",
                (program_id, current),
            ).fetchone()
            state = ReviewState.model_validate(parent["review"])
            if state.review_id != review_id:
                raise DomainError(409, "Версия не принадлежит редактируемой сверке")
            choices = {item.conflict_id: item for item in state.decisions}
            changed = set()
            for selection in selections:
                row = Repository.require(
                    cur.execute(
                        "SELECT job_id,rule_code,details FROM validation_issues WHERE "
                        "program_id=%s AND id=%s",
                        (program_id, selection.conflict_id),
                    ).fetchone()
                )
                if row["job_id"] != review_id:
                    raise DomainError(409, "Вариант относится к другой сверке")
                if getattr(body, "check_job_id", None) not in {None, row["job_id"]}:
                    raise DomainError(404, "Вариант относится к другой проверке")
                details = row["details"]
                if getattr(selection, "custom_value", None) is not None:
                    variant_id = uuid5(review_id, f"custom:{selection.conflict_id}")
                    changes = custom_decision_changes(
                        details, selection.custom_value, rule_code=row["rule_code"]
                    )
                else:
                    variant = next(
                        (
                            item
                            for item in details["variants"]
                            if item["id"] == str(selection.variant_id)
                        ),
                        None,
                    )
                    if not details["requires_decision"] or variant is None:
                        raise DomainError(422, "Выберите один из предложенных вариантов")
                    variant_id, changes = selection.variant_id, variant["changes"]
                if any(item.get("op") not in DOCUMENT_MUTATION_OPS for item in changes):
                    raise DomainError(422, "Вариант не является документным")
                if (
                    selection.conflict_id in choices
                    and choices[selection.conflict_id].variant_id == variant_id
                    and choices[selection.conflict_id].changes == changes
                ):
                    continue
                choices[selection.conflict_id] = ReviewDecision(
                    conflict_id=selection.conflict_id,
                    variant_id=variant_id,
                    check_job_id=review_id,
                    changes=changes,
                    decided_at=datetime.now(UTC),
                    author=body.signature,
                )
                changed.add(selection.conflict_id)
            if not changed:
                return {"id": current, "number": parent["number"]}
            updated = state.model_copy(
                update={
                    "decisions": list(choices.values()),
                    "projected_decision_ids": [
                        item for item in state.projected_decision_ids if item not in changed
                    ],
                }
            ).model_dump(mode="json")
            identity = uuid4()
            result = cur.execute(
                "INSERT INTO "
                "program_versions(id,program_id,number,parent_version_id,graph_state_id,review,"
                "origin,operation_key,request_hash) "
                "VALUES(%s,%s,%s,%s,%s,%s,'save',%s,%s) RETURNING id,number",
                (
                    identity,
                    program_id,
                    parent["number"] + 1,
                    current,
                    parent["graph_state_id"],
                    Jsonb(updated),
                    key,
                    fingerprint,
                ),
            ).fetchone()
            journal(
                cur,
                program_id,
                identity,
                {"review": parent["review"]},
                {"review": updated},
                actor_type="user",
                actor_id=body.signature,
            )
            cur.execute(
                "UPDATE programs SET current_version_id=%s WHERE id=%s", (identity, program_id)
            )
            return result

    def checkpoint_choices(self, program_id, review_id, version_id):
        """Поставить одну устойчивую сборку накопленных решений по кнопке «Далее»."""
        from app.version_policy import pending_document_choices

        with self.pool.connection() as conn, conn.cursor(row_factory=dict_row) as cur:
            current = self.lock(cur, program_id)
            if current != version_id:
                raise DomainError(409, "Текущая версия изменилась")
            row = Repository.require(
                cur.execute(
                    "SELECT review FROM program_versions WHERE program_id=%s AND id=%s",
                    (program_id, current),
                ).fetchone()
            )
            review = ReviewState.model_validate(row["review"])
            if review.review_id != review_id:
                raise DomainError(409, "Недопустимая сверка")
            if not pending_document_choices(review):
                return {"graph_build": None, "version_id": str(current)}
            old = cur.execute(
                "SELECT id,report FROM processing_jobs WHERE program_id=%s AND "
                "parameters->>'purpose'='build_graph' "
                "AND input_version_id=%s ORDER BY created_at DESC LIMIT 1",
                (program_id, current),
            ).fetchone()
            if old and old["report"].get("outcome") not in {"failed", "conflict"}:
                identity = old["id"]
            else:
                identity = self.enqueue_graph_build(cur, program_id, review_id, current)
            return {"graph_build": str(identity), "version_id": str(current)}

    def _decide_automatic(self, program_id, review_id, key, body, fingerprint):
        """Подготовить тяжёлую проекцию без блокировки, затем атомарно записать версию."""
        from app.source_graph import apply_document_selections

        selections = body.selections if isinstance(body, DecisionBatchCreate) else [body]
        with self.pool.connection() as connection, connection.cursor(row_factory=dict_row) as cur:
            previous = previous_version(cur, program_id, key, fingerprint)
            if previous:
                return previous
            current = Repository.require(cur.execute(
                "SELECT current_version_id FROM programs WHERE id=%s", (program_id,),
            ).fetchone())["current_version_id"]
            if current != body.expected_version_id:
                raise DomainError(409, "Текущая версия изменилась")
            version = self.version(cur, program_id, current)
            snapshot = self.checked_snapshot(version, review_id)
            review = self.require_review(cur, program_id, review_id)
            existing = {item.conflict_id: item for item in snapshot.review.decisions}
            decisions = []
            for selection in selections:
                row = Repository.require(cur.execute(
                    "SELECT job_id,rule_code,details FROM validation_issues "
                    "WHERE program_id=%s AND id=%s",
                    (program_id, selection.conflict_id),
                ).fetchone())
                if row["job_id"] != review_id:
                    raise DomainError(409, "Документный вариант относится к другой подготовке")
                if getattr(body, "check_job_id", None) not in {None, row["job_id"]}:
                    raise DomainError(404, "Вариант относится к другой проверке")
                details = row["details"]
                if getattr(selection, "custom_value", None) is not None:
                    variant_id = uuid5(review_id, f"custom:{selection.conflict_id}")
                    changes = custom_decision_changes(
                        details, selection.custom_value, rule_code=row["rule_code"],
                    )
                else:
                    variant = next((
                        item for item in details["variants"]
                        if item["id"] == str(selection.variant_id)
                    ), None)
                    if not details["requires_decision"] or variant is None:
                        raise DomainError(422, "Выберите один из предложенных вариантов")
                    variant_id, changes = selection.variant_id, variant["changes"]
                if any(item.get("op") not in DOCUMENT_MUTATION_OPS for item in changes):
                    raise DomainError(422, "Вариант не является документным изменением")
                old = existing.get(selection.conflict_id)
                if old and old.variant_id == variant_id and old.changes == changes:
                    continue
                decisions.append(ReviewDecision(
                    conflict_id=selection.conflict_id, variant_id=variant_id,
                    check_job_id=review_id, changes=changes, decided_at=datetime.now(UTC),
                    author=body.signature,
                ))
        if not decisions:
            return version
        try:
            next_snapshot = apply_document_selections(
                program_id, review["parameters"]["input"]["documents"], snapshot, decisions,
            )
        except ValueError as error:
            raise DomainError(409, "Нельзя однозначно применить вариант: " + str(error)) from error
        with self.pool.connection() as connection, connection.cursor(row_factory=dict_row) as cur:
            current = self.lock(cur, program_id)
            previous = previous_version(cur, program_id, key, fingerprint)
            if previous:
                return previous
            if current != body.expected_version_id:
                raise DomainError(409, "Текущая версия изменилась во время подготовки выбора")
            version = insert_version(
                cur, program_id, current, uuid4(), next_snapshot,
                key=key, fingerprint=fingerprint, actor_type="user",
                actor_id=body.signature, before_snapshot=snapshot,
            )
            self.create(
                program_id, "automatic-check:" + str(version["id"]),
                CheckCreate(version_id=version["id"]), purpose="check", review_id=review_id,
                connection=connection, snapshot_override=next_snapshot, review_override=review,
            )
            return version

    def decide(self, program_id, review_id, key, body, *, defer_graph=False):
        fingerprint = digest(
            {
                "operation": "decision",
                "review_id": str(review_id),
                "body": body.model_dump(mode="json"),
            }
        )
        if defer_graph:
            return self._save_document_choices(program_id, review_id, key, body, fingerprint)
        return self._decide_automatic(program_id, review_id, key, body, fingerprint)

    def decide_many(
        self, program_id, review_id, key, body: DecisionBatchCreate, *, defer_graph=False
    ):
        fingerprint = digest(
            {
                "operation": "decision_batch",
                "review_id": str(review_id),
                "body": body.model_dump(mode="json"),
            }
        )
        if defer_graph:
            return self._save_document_choices(program_id, review_id, key, body, fingerprint)
        return self._decide_automatic(program_id, review_id, key, body, fingerprint)

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


    def compact_version(self, program_id, version_id):
        """Прочитать только поля версии, нужные экрану замечаний."""
        with self.pool.connection() as connection, connection.cursor(row_factory=dict_row) as cur:
            row = cur.execute(
                """SELECT v.id,v.program_id,v.number,v.parent_version_id,
                          v.restored_from_version_id,v.origin,v.created_at,
                          s.id AS graph_state_id,s.status AS graph_status,
                          s.content_hash AS graph_hash,
                          jsonb_build_object(
                            'schema_version',COALESCE(s.schema_version,2),
                            'metadata',COALESCE(s.metadata-'__parsed_documents','{}'::jsonb),
                            'document_ids',COALESCE((
                              SELECT jsonb_agg(d.document_id ORDER BY d.position)
                              FROM graph_documents d WHERE d.graph_state_id=s.id
                            ),'[]'::jsonb),
                            'entities','[]'::jsonb,'relations','[]'::jsonb,
                            'source_refs','[]'::jsonb,'source_links','[]'::jsonb,
                            'review',v.review) AS snapshot
                   FROM program_versions v LEFT JOIN graph_states s ON s.id=v.graph_state_id
                   WHERE v.program_id=%s AND v.id=%s""",
                (program_id, version_id),
            ).fetchone()
        return Repository.require(row)

    def issue_source_refs(self, page_issues):
        """Вернуть только источники замечаний текущей страницы."""
        reference_ids = sorted({
            str(identity)
            for issue in page_issues
            for identity in issue.get("source_ref_ids", [])
        })
        version_ids = sorted({
            str(issue["source_version_id"])
            for issue in page_issues if issue.get("source_version_id")
        })
        if not reference_ids or not version_ids:
            return []
        with self.pool.connection() as connection, connection.cursor(row_factory=dict_row) as cur:
            rows = cur.execute(
                """SELECT DISTINCT r.id,r.document_id,r.section,r.locator
                   FROM program_versions v JOIN graph_sources g ON g.graph_state_id=v.graph_state_id
                   JOIN source_refs r ON r.id=g.source_ref_id
                   WHERE v.id=ANY(%s::uuid[]) AND r.id=ANY(%s::uuid[])
                   ORDER BY r.id""",
                (version_ids, reference_ids),
            ).fetchall()
        return [{
            "id": str(item["id"]), "document_id": str(item["document_id"]),
            "section": item["section"], "locator": item["locator"],
        } for item in rows]

    def details(
        self, program_id, version_id, limit, offset, *, include_graph=True,
        include_summary=True,
    ):
        row = (
            Repository(self.pool).version(program_id, version_id)
            if include_graph else self.compact_version(program_id, version_id)
        )
        if row["snapshot"].get("schema_version") != 2:
            raise DomainError(404, "У версии нет контекста подготовки")
        snapshot = SnapshotV2.model_validate(row["snapshot"]) if include_graph else None
        snapshot_data = row["snapshot"]
        review_state = snapshot.review if snapshot else ReviewState.model_validate(
            snapshot_data.get("review")
        ) if snapshot_data.get("review") else None
        if review_state is None:
            raise DomainError(404, "У версии нет контекста подготовки")
        metadata = snapshot.metadata if snapshot else snapshot_data["metadata"]
        review = (
            self.job(program_id, review_state.review_id)
            if include_graph else self.compact_job(
                program_id, review_state.review_id, include_summary=include_summary,
            )
        )
        graph_build = None
        choices = {
            str(item.conflict_id): item.model_dump(mode="json")
            for item in review_state.decisions
        }
        with self.pool.connection() as connection, connection.cursor(row_factory=dict_row) as cur:
            lifecycle = None
            manual_targets = {"rpd": [], "competencies": []}
            if review_state:

                automatic_check = cur.execute(
                    "SELECT id,program_id,input_version_id,parameters,report FROM processing_jobs "
                    "WHERE program_id=%s AND input_version_id=%s AND type='validate' "
                    "AND parameters->>'purpose'='check' "
                    "AND parameters->>'manual_rule_codes' IS NULL "
                    "ORDER BY created_at DESC,id DESC LIMIT 1",
                    (program_id, version_id),
                ).fetchone()
                if include_graph:
                    from app.version_policy import version_capabilities

                    lifecycle = version_capabilities(
                        snapshot, program_id, version_id, automatic_check,
                        review["report"].get("outcome") or "processing",
                    )
                else:
                    from app.version_policy import compact_version_capabilities

                    lifecycle = compact_version_capabilities(
                        review_state, metadata, row["graph_status"], row["graph_hash"],
                        program_id, version_id, automatic_check,
                        review["report"].get("outcome") or "processing",
                    )
            if review_state.graph_ready is False or lifecycle:
                build_job = cur.execute(
                    """SELECT id,input_version_id,output_version_id,report
                       FROM processing_jobs WHERE program_id=%s AND type='validate'
                       AND parameters->>'purpose'='build_graph'
                       AND parameters->>'pending_version_id'=%s
                       ORDER BY created_at DESC,id DESC LIMIT 1""",
                    (program_id, str(version_id)),
                ).fetchone()
                if build_job:
                    report = build_job["report"]
                    graph_build = {
                        "id": build_job["id"],
                        "pending_version_id": build_job["input_version_id"],
                        "output_version_id": build_job["output_version_id"],
                        "status": report.get("outcome")
                        or report.get("status")
                        or "pending",
                        "message": report.get("message"),
                    }
            latest_columns = "*" if include_summary else "id"
            latest = cur.execute(
                f"""SELECT {latest_columns} FROM processing_jobs
                   WHERE program_id=%s AND input_version_id=%s
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
            fresh_issue_ids = (
                [str(item["id"]) for item in latest["report"].get("result", {}).get("issues", [])]
                if latest and include_summary else []
            )
            if review_state.graph_ready and include_summary:
                manual_targets = (
                    manual_check_targets(snapshot, review["parameters"]["input"]["documents"])
                    if include_graph else compact_manual_check_targets(cur, row["graph_state_id"])
                )
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
            # Полная проверка заменяет диагностику, но в новом процессе не скрывает
            # сохранённые карточки выбора: первый экран остаётся доступным.
            issues = visible_prepare_findings(
                issues, review["id"],
            )
        build_errors = review["report"].get("result", {}).get("graph_build_issues", [])
        if review_state.graph_ready is False:
            for item in build_errors:
                issue_id = uuid5(
                    review_state.review_id,
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
                        "job_id": review_state.review_id,
                    }
                )
        if lifecycle:
            from app.version_findings import automatic_findings

            issues = automatic_findings(
                issues, metadata, review_state.review_id, version_id,
                lifecycle["analysis_status"], lifecycle["check_status"],
            )
        page_issues = [
            {
                **item,
                "effective_status": item.get("effective_status") or ("resolved"
                if item["id"] in choices
                else item["status"]),
                "decision": choices.get(item["id"]),
            }
            for item in issues[offset : offset + limit]
        ]
        if not include_graph:
            snapshot_data["source_refs"] = self.issue_source_refs(page_issues)
        if not include_summary and not include_graph:
            snapshot_data = {
                "schema_version": 2, "metadata": {}, "document_ids": [],
                "entities": [], "relations": [],
                "source_refs": snapshot_data["source_refs"], "source_links": [], "review": None,
            }
        version_output = row if include_graph else {
            key: row[key] for key in (
                "id", "program_id", "number", "parent_version_id",
                "restored_from_version_id", "origin", "created_at",
            )
        }
        if not include_graph:
            version_output["snapshot"] = snapshot_data
        return {
            "version": version_output,
            "lifecycle": lifecycle if include_summary else None,
            "graph_build": graph_build,
            "conflicts_count": sum(i["requires_decision"] for i in issues),
            "issues_count": len(issues),
            "issues": page_issues,
            "confirmed": False,
            "confirmation": None,
            "check_job_id": latest["id"] if latest and include_summary else None,
            "fresh_issue_ids": fresh_issue_ids if include_summary else [],
            "check_report": (
                project_legacy_review_result(latest["report"]["result"])
                if latest and include_summary
                else None
            ),
            "manual_check_targets": (
                manual_targets if include_summary else {"rpd": [], "competencies": []}
            ),
            "prepare_report": (
                project_prepare_report(review["report"]["result"])
                if include_summary else None
            ),
        }
