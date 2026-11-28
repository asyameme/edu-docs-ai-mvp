"""Чтение компактных замечаний закреплённой версии без загрузки всего графа."""

from uuid import UUID

from psycopg.rows import dict_row

from app.chat_findings import build_findings_page
from app.errors import DomainError


def read_findings_page(pool, program_id, version_id, *, limit=10, after=None, status="all"):
    with pool.connection() as conn, conn.cursor(row_factory=dict_row) as cur:
        version = cur.execute(
            "SELECT review FROM program_versions WHERE program_id=%s AND id=%s",
            (program_id, version_id),
        ).fetchone()
        if version is None:
            raise DomainError(404, "Версия программы не найдена")
        review = version["review"] or {}
        review_id = review.get("review_id")
        latest = cur.execute(
            """SELECT id FROM processing_jobs WHERE program_id=%s AND input_version_id=%s
               AND type='validate' AND parameters->>'purpose'='check'
               AND parameters->>'manual_rule_codes' IS NULL
               AND report->>'outcome'='completed' ORDER BY created_at DESC,id DESC LIMIT 1""",
            (program_id, version_id),
        ).fetchone()
        check_id = latest["id"] if latest else None
        job_ids = [UUID(str(j)) for j in (review_id, check_id) if j]
        rows = cur.execute(
            """SELECT id,job_id,rule_code,type,severity,status,
                      details->>'message' AS message,
                      details->'source_ref_ids' AS source_ref_ids,
                      details->'source_document_ids' AS source_document_ids,
                      details->'requires_decision' AS requires_decision,details
               FROM validation_issues WHERE program_id=%s
               AND ((%s AND job_id=ANY(%s)) OR (NOT %s AND version_id=%s))
               ORDER BY created_at DESC,id""",
            (program_id, bool(job_ids), job_ids, bool(job_ids), version_id),
        ).fetchall()
        decisions = {str(d["conflict_id"]) for d in review.get("decisions", [])}
        selected = []
        for row in rows:
            identity = str(row["id"])
            if (
                check_id
                and str(row["job_id"]) == str(review_id)
                and row["rule_code"].startswith("R-08")
                and (not row["requires_decision"] or identity in decisions)
            ):
                continue
            row["effective_status"] = (
                "resolved"
                if identity in decisions
                else row["status"]
            )
            row["source_ref_ids"] = row["source_ref_ids"] or []
            row["source_document_ids"] = row["source_document_ids"] or []
            selected.append(row)
        if review_id:
            from app.version_findings import read_automatic_findings

            selected = read_automatic_findings(
                cur, program_id, version_id, review,
                [{**row["details"], **row} for row in selected],
            )
        refs = sorted({UUID(str(r)) for row in selected for r in row["source_ref_ids"]})
        references = (
            {
                str(r["id"]): str(r["document_id"])
                for r in cur.execute(
                    "SELECT id,document_id FROM source_refs WHERE program_id=%s AND id=ANY(%s)",
                    (program_id, refs),
                ).fetchall()
            }
            if refs
            else {}
        )
        docs = {UUID(d) for d in references.values()} | {
            UUID(str(d)) for row in selected for d in row["source_document_ids"]
        }
        documents = (
            {
                str(d["id"]): d["original_name"]
                for d in cur.execute(
                    "SELECT id,original_name FROM source_documents "
                    "WHERE program_id=%s AND id=ANY(%s)",
                    (program_id, sorted(docs)),
                ).fetchall()
            }
            if docs
            else {}
        )
    try:
        return build_findings_page(
            selected,
            version_id=version_id,
            documents=documents,
            references=references,
            limit=limit,
            after=after,
            status=status,
        )
    except ValueError as error:
        raise DomainError(422, str(error)) from error
