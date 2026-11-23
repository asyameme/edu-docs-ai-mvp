"""Компактный состав состояния и переиспользуемые свидетельства редакций."""

from collections import defaultdict
from uuid import UUID, uuid5

from app.review_logic import digest


def _identity(program, kind, value):
    return uuid5(UUID(str(program)), kind + ":" + digest(value))


def _fresh(cur, table, rows):
    """Не выполняем даже конфликтующую вставку для уже сохранённого набора."""
    from app.graph_storage import _chunks

    known = set()
    for chunk in _chunks([row[0] for row in rows]):
        known.update(
            row["id"]
            for row in cur.execute(f"SELECT id FROM {table} WHERE id=ANY(%s)", (chunk,)).fetchall()
        )
    fresh = [row for row in rows if row[0] not in known]
    if fresh:
        # Одна команда позволяет проверить весь вставленный набор соединениями.
        with cur.copy(f"COPY {table} FROM STDIN") as stream:
            for row in fresh:
                stream.write_row(row)


class GraphComposition:
    def __init__(self, cur, program):
        self.cur = cur
        self.program = program
        self.rows = defaultdict(list)

    @classmethod
    def for_cursor(cls, cur, program):
        # Миграция 0006 вызывает текущий код в промежуточной старой схеме.
        enabled = cur.execute(
            "SELECT current_schema()='public' AND "
            "to_regclass('public.graph_state_contents') IS NOT NULL AS enabled"
        ).fetchone()["enabled"]
        return cls(cur, program) if enabled else None

    def add(self, table, rows):
        self.rows[table].extend(rows)

    def persist(self, state, *, cache=True):
        cur, program = self.cur, self.program
        entities = self.rows["graph_entities"]
        relations = self.rows["graph_relations"]
        owners = {
            (kind, str(row[2])): row[3]
            for kind, rows in (("entity", entities), ("relation", relations))
            for row in rows
        }
        selections = []
        for row in self.rows["graph_documents"]:
            _, _, document, artifact, member, _ = row
            identity = _identity(
                program,
                "document-selection",
                [str(document), str(artifact) if artifact else None, member],
            )
            selections.append((identity, program, document, artifact, "parsed_json", member))
        _fresh(cur, "graph_document_selections", selections)

        grouped = defaultdict(list)
        links = self.rows["graph_source_links"]
        for row in links:
            kind, target = ("entity", row[3]) if row[3] is not None else ("relation", row[4])
            grouped[(kind, str(owners[(kind, str(target))]))].append(row)
        evidence = []
        for (kind, revision), items in grouped.items():
            facts = [UUID(str(row[2])) for row in items]
            public = [UUID(str(row[7])) for row in items]
            identity = _identity(
                program,
                "revision-evidence",
                [kind, revision, [str(item) for item in facts], [str(item) for item in public]],
            )
            evidence.append(
                (
                    identity,
                    program,
                    UUID(revision) if kind == "entity" else None,
                    UUID(revision) if kind == "relation" else None,
                    facts,
                    public,
                )
            )
        _fresh(cur, "revision_evidence_sets", evidence)

        order = [UUID(str(row[7])) for row in links]
        order_id = (
            _identity(program, "evidence-order", [str(item) for item in order]) if order else None
        )
        if order:
            _fresh(cur, "graph_evidence_orders", [(order_id, program, order)])
        cur.execute(
            "INSERT INTO graph_state_contents(graph_state_id,program_id,entity_revision_ids,"
            "relation_revision_ids,document_selection_ids,source_ref_ids,evidence_set_ids,"
            "evidence_order_id) VALUES(%s,%s,%s,%s,%s,%s,%s,%s)",
            (
                state,
                program,
                [row[3] for row in entities],
                [row[3] for row in relations],
                [row[0] for row in selections],
                [UUID(str(row[2])) for row in self.rows["graph_sources"]],
                [row[0] for row in evidence],
                order_id,
            ),
        )
        if cache:
            cur.execute("INSERT INTO graph_snapshot_cache(graph_state_id) VALUES(%s)", (state,))


def write_members(cur, composition, table, sql, rows):
    if composition is None:
        from app.graph_storage import _insert_many

        _insert_many(cur, sql, rows)
    else:
        composition.add(table, rows)


def warm_local_review(cur, program, job, documents, output_id):
    """Подготовить общие атомы локальных документов до публикации решений."""
    if job["parameters"].get("executor") != "local":
        return
    if GraphComposition.for_cursor(cur, program) is None:
        return
    if cur.execute(
        "SELECT 1 FROM graph_states WHERE producing_job_id=%s AND status='sealed' LIMIT 1",
        (job["id"],),
    ).fetchone():
        return
    from app.document_adapter import to_snapshot_v2
    from app.domain import SnapshotV2
    from app.graph_storage import save_graph

    files = [{"document_id": identity, "output": value} for identity, value in documents.items()]
    data, _ = to_snapshot_v2(program, files, original_files=files)
    data["review"] = {
        "review_id": str(job["id"]),
        "import_job_id": job["parameters"]["import_job_id"],
        "analysis_version_id": str(output_id),
        "graph_ready": True,
        "decisions": [],
        "confirmation": None,
    }
    save_graph(cur, program, SnapshotV2.model_validate(data), producer=job["id"])
