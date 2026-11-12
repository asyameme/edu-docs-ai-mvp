"""Неизменяемые редакции, состав графа, артефакты и журнал в одной транзакции."""

import hashlib
from copy import deepcopy
from uuid import UUID, uuid4, uuid5

import rfc8785
from psycopg.types.json import Jsonb

from app.domain import SnapshotV2
from app.errors import DomainError
from app.review_logic import content_hash, digest


def canonical_bytes(value):
    return rfc8785.dumps(value)


def parsed_artifact(cur, program_id, document_id, output, job_id, attempt):
    raw = canonical_bytes(output)
    checksum = hashlib.sha256(raw).hexdigest()
    identity = uuid5(UUID(str(program_id)), f"parsed:{job_id}:{attempt}:{document_id}:{checksum}")
    cur.execute(
        """INSERT INTO document_artifacts(id,program_id,document_id,producing_job_id,
        producing_attempt,kind,parser_revision,parser_config,parser_config_hash,hash_encoding,
        parsed_output,sha256,size_bytes) VALUES(%s,%s,%s,%s,%s,'parsed_json',%s,%s,%s,
        'jcs-rfc8785-utf8',%s,%s,%s) ON CONFLICT(id) DO NOTHING""",
        (
            identity,
            program_id,
            document_id,
            job_id,
            attempt,
            str(output.get("meta", {}).get("parser_version") or "unreported"),
            Jsonb({"config_reported": False}),
            hashlib.sha256(canonical_bytes({"config_reported": False})).hexdigest(),
            Jsonb(output),
            checksum,
            len(raw),
        ),
    )
    return identity


def artifact_output(cur, artifact_id):
    row = cur.execute(
        "SELECT parsed_output,sha256,size_bytes FROM document_artifacts "
        "WHERE id=%s AND kind='parsed_json'",
        (artifact_id,),
    ).fetchone()
    if row is None:
        raise DomainError(422, "Артефакт разбора не найден")
    raw = canonical_bytes(row["parsed_output"])
    if len(raw) != row["size_bytes"] or hashlib.sha256(raw).hexdigest() != row["sha256"]:
        raise DomainError(422, "Хэш JSON артефакта не совпал")
    return row["parsed_output"]


def hydrate_report(cur, report):
    result = deepcopy(report)
    for item in result.get("files", {}).values():
        if item.get("artifact_id"):
            item["output"] = artifact_output(cur, item["artifact_id"])
    return result


def pack_tree(cur, program_id, value):
    """Графы и JSON документов в сообщениях заменяются неизменяемыми ссылками."""
    if isinstance(value, list):
        return [pack_tree(cur, program_id, item) for item in value]
    if not isinstance(value, dict):
        return value
    if (
        value.get("schema_version") in (1, 2)
        and {"entities", "relations", "source_refs", "document_ids"} <= value.keys()
    ):
        state = save_graph(cur, program_id, value)
        return {"__graph_state": str(state), "review": value.get("review")}
    if "document_id" in value and isinstance(value.get("parsed"), dict):
        checksum = hashlib.sha256(canonical_bytes(value["parsed"])).hexdigest()
        row = cur.execute(
            "SELECT id FROM document_artifacts WHERE program_id=%s AND document_id=%s "
            "AND kind='parsed_json' AND sha256=%s ORDER BY created_at,id LIMIT 1",
            (program_id, value["document_id"], checksum),
        ).fetchone()
        if row is None:
            raise DomainError(422, "Вход анализа не связан с сохранённым разбором")
        value = {**value, "parsed": {"__parsed_artifact": str(row["id"])}}
    literal = {
        "metadata",
        "attributes",
        "observed_value",
        "parser_config",
        "ruleset",
        "value",
        "changes",
    }
    return {
        key: item if key in literal else pack_tree(cur, program_id, item)
        for key, item in value.items()
    }


def unpack_tree(cur, value):
    if isinstance(value, list):
        return [unpack_tree(cur, item) for item in value]
    if not isinstance(value, dict):
        return value
    if set(value) == {"__graph_state", "review"}:
        return load_graph(cur, value["__graph_state"], value["review"])
    if set(value) == {"__parsed_artifact"}:
        return artifact_output(cur, value["__parsed_artifact"])
    literal = {
        "metadata",
        "attributes",
        "observed_value",
        "parser_config",
        "ruleset",
        "value",
        "changes",
    }
    return {key: item if key in literal else unpack_tree(cur, item) for key, item in value.items()}


def hydrate_job(cur, row):
    if row:
        row = dict(row)
        row["parameters"] = unpack_tree(cur, row["parameters"])
        row["report"] = unpack_tree(cur, row["report"])
        if row["type"] == "import":
            row["report"] = hydrate_report(cur, row["report"])
            if row.get("output_graph_state_id"):
                row["report"]["snapshot"] = load_graph(cur, row["output_graph_state_id"])
    return row


def load_graph(cur, state_id, review=None):
    row = cur.execute(
        "SELECT graph_snapshot(%s,%s) AS snapshot", (state_id, Jsonb(review))
    ).fetchone()
    if row is None or row["snapshot"] is None:
        raise DomainError(404, "Состояние графа не найдено")
    return row["snapshot"]


def save_graph(cur, program_id, snapshot, *, producer=None, attempt=None, members=None):
    data = (
        snapshot.model_dump(mode="json") if hasattr(snapshot, "model_dump") else deepcopy(snapshot)
    )
    schema = data.get("schema_version", 1)
    if schema == 2:
        data = SnapshotV2.model_validate(data).model_dump(mode="json")
    metadata = deepcopy(data.get("metadata", {}))
    if "__parsed_documents" in metadata:
        raise DomainError(422, "Служебное поле metadata зарезервировано")
    parsed = metadata.pop("parsed_documents", None)
    if parsed is not None:
        if not isinstance(parsed, dict) or not set(parsed).issubset(set(data["document_ids"])):
            raise DomainError(422, "JSON документов не соответствует составу графа")
        metadata["__parsed_documents"] = True
    graph_hash = content_hash(data) if schema == 2 else digest(data)
    members = dict(members or {})
    source_import = (data.get("review") or {}).get("import_job_id")
    if source_import:
        rows = cur.execute(
            "SELECT d.document_id,d.member_name FROM graph_documents d "
            "JOIN graph_states s ON s.id=d.graph_state_id WHERE s.producing_job_id=%s "
            "ORDER BY s.created_at,s.id",
            (source_import,),
        ).fetchall()
        for row in rows:
            members.setdefault(str(row["document_id"]), row["member_name"])
    fallback = {}
    if parsed is None:
        imported = (data.get("review") or {}).get("import_job_id")
        for document in data["document_ids"]:
            found = cur.execute(
                "SELECT id FROM document_artifacts WHERE program_id=%s AND document_id=%s "
                "AND kind='parsed_json' AND (%s::uuid IS NULL OR producing_job_id=%s) "
                "ORDER BY created_at DESC,id LIMIT 1",
                (program_id, document, imported, imported),
            ).fetchone()
            fallback[document] = found["id"] if found else None
    # Review хранится у версии и не влияет на состав графа.
    previous = cur.execute(
        "SELECT id FROM graph_states WHERE program_id=%s AND content_hash=%s "
        "AND status='sealed' ORDER BY created_at,id",
        (program_id, graph_hash),
    ).fetchall()
    expected = {k: v for k, v in data.items() if k != "review"}
    for row in previous:
        candidate = load_graph(cur, row["id"])
        selected_members = cur.execute(
            "SELECT document_id,member_name FROM graph_documents WHERE graph_state_id=%s",
            (row["id"],),
        ).fetchall()
        if any(
            members.get(str(item["document_id"]), item["member_name"]) != item["member_name"]
            for item in selected_members
        ):
            continue
        if parsed is None:
            selected = cur.execute(
                "SELECT document_id,parsed_artifact_id FROM graph_documents "
                "WHERE graph_state_id=%s",
                (row["id"],),
            ).fetchall()
            if {
                str(item["document_id"]): item["parsed_artifact_id"] for item in selected
            } != fallback:
                continue
        if (
            digest({k: v for k, v in candidate.items() if k != "review"}) == digest(expected)
            and producer is None
        ):
            return row["id"]
    state = uuid4()
    cur.execute(
        "INSERT INTO graph_states(id,program_id,schema_version,metadata,producing_job_id,"
        "producing_attempt) VALUES(%s,%s,%s,%s,%s,%s)",
        (state, program_id, schema, Jsonb(metadata), producer, attempt),
    )
    for position, document in enumerate(data["document_ids"]):
        original = cur.execute(
            "SELECT original_name FROM source_documents WHERE program_id=%s AND id=%s",
            (program_id, document),
        ).fetchone()
        if original is None:
            raise DomainError(422, "Источник отсутствует или принадлежит другой программе")
        artifact = fallback.get(document)
        if parsed and document in parsed:
            checksum = hashlib.sha256(canonical_bytes(parsed[document])).hexdigest()
            found = None
            if producer:
                selected = cur.execute(
                    "SELECT report->'files'->%s->>'artifact_id' AS id FROM "
                    "processing_jobs WHERE id=%s",
                    (document, producer),
                ).fetchone()
                if selected and selected["id"]:
                    found = cur.execute(
                        "SELECT id FROM document_artifacts WHERE program_id=%s "
                        "AND document_id=%s AND id=%s AND sha256=%s",
                        (program_id, document, selected["id"], checksum),
                    ).fetchone()
            found = (
                found
                or cur.execute(
                    "SELECT id FROM document_artifacts WHERE program_id=%s AND document_id=%s "
                    "AND kind='parsed_json' AND sha256=%s ORDER BY created_at,id LIMIT 1",
                    (program_id, document, checksum),
                ).fetchone()
            )
            if found:
                artifact = found["id"]
            elif producer or (data.get("review") or {}).get("review_id"):
                artifact = parsed_artifact(
                    cur,
                    program_id,
                    document,
                    parsed[document],
                    producer or data["review"]["review_id"],
                    attempt if producer else 1,
                )
            else:
                raise DomainError(422, "У JSON документа нет сохранённого результата импорта")
        if document not in members and artifact:
            named = cur.execute(
                "SELECT member_name FROM graph_documents WHERE program_id=%s "
                "AND document_id=%s AND parsed_artifact_id=%s ORDER BY "
                "graph_state_id LIMIT 1",
                (program_id, document, artifact),
            ).fetchone()
            if named:
                members[document] = named["member_name"]
        cur.execute(
            "INSERT INTO graph_documents(program_id,graph_state_id,document_id,parsed_artifact_id,"
            "member_name,position) VALUES(%s,%s,%s,%s,%s,%s)",
            (
                program_id,
                state,
                document,
                artifact,
                members.get(document, original["original_name"]),
                position,
            ),
        )
    for position, ref in enumerate(data["source_refs"]):
        existing = cur.execute(
            "SELECT document_id,artifact_id,section,locator,artifact_locator "
            "FROM source_refs WHERE id=%s",
            (ref["id"],),
        ).fetchone()
        values = {
            key: ref.get(key)
            for key in ("document_id", "artifact_id", "section", "locator", "artifact_locator")
        }
        if existing:
            if digest(
                {k: str(v) if isinstance(v, UUID) else v for k, v in existing.items()}
            ) != digest(values):
                raise DomainError(422, "Правка источника требует нового ID")
        else:
            cur.execute(
                "INSERT INTO "
                "source_refs(id,program_id,document_id,artifact_id,section,locator,"
                "artifact_locator) "
                "VALUES(%s,%s,%s,%s,%s,%s,%s)",
                (
                    ref["id"],
                    program_id,
                    ref["document_id"],
                    ref.get("artifact_id"),
                    ref.get("section"),
                    ref["locator"],
                    ref.get("artifact_locator"),
                ),
            )
        cur.execute(
            "INSERT INTO graph_sources VALUES(%s,%s,%s,%s,%s)",
            (program_id, state, ref["id"], ref["document_id"], position),
        )
    for kind, items in (("entity", data["entities"]), ("relation", data["relations"])):
        for position, item in enumerate(items):
            rev = uuid5(UUID(str(program_id)), kind + ":" + digest(item))
            table = "entity_revisions" if kind == "entity" else "relation_revisions"
            exists = cur.execute(f"SELECT 1 FROM {table} WHERE revision_id=%s", (rev,)).fetchone()
            if not exists:
                if kind == "entity":
                    cur.execute(
                        "INSERT INTO "
                        "entity_revisions(revision_id,program_id,entity_id,kind,attributes) "
                        "VALUES(%s,%s,%s,%s,%s)",
                        (rev, program_id, item["id"], item["kind"], Jsonb(item["attributes"])),
                    )
                else:
                    cur.execute(
                        "INSERT INTO "
                        "relation_revisions(revision_id,program_id,relation_id,source_entity_id,"
                        "target_entity_id,type,confidence,attributes) "
                        "VALUES(%s,%s,%s,%s,%s,%s,%s,%s)",
                        (
                            rev,
                            program_id,
                            item["id"],
                            item["source_id"],
                            item["target_id"],
                            item["type"],
                            item.get("confidence"),
                            Jsonb(item.get("attributes", {})),
                        ),
                    )
                for order, ref_id in enumerate(item["source_ref_ids"]):
                    cur.execute(
                        "INSERT INTO revision_sources VALUES(%s,%s,%s,%s,%s)",
                        (
                            program_id,
                            rev if kind == "entity" else None,
                            rev if kind == "relation" else None,
                            ref_id,
                            order,
                        ),
                    )
            if kind == "entity":
                cur.execute(
                    "INSERT INTO graph_entities VALUES(%s,%s,%s,%s,%s)",
                    (program_id, state, item["id"], rev, position),
                )
            else:
                cur.execute(
                    "INSERT INTO graph_relations VALUES(%s,%s,%s,%s,%s,%s,%s)",
                    (
                        program_id,
                        state,
                        item["id"],
                        rev,
                        item["source_id"],
                        item["target_id"],
                        position,
                    ),
                )
    for position, link in enumerate(data.get("source_links", [])):
        fact_id = uuid5(UUID(str(program_id)), "source-link-fact:" + digest(link))
        entity = link["target_id"] if link["target_kind"] == "entity" else None
        relation = link["target_id"] if link["target_kind"] == "relation" else None
        previous = cur.execute(
            "SELECT entity_id,relation_id,source_ref_id,field_path,observed_value "
            "FROM source_links WHERE id=%s",
            (fact_id,),
        ).fetchone()
        values = dict(
            entity_id=entity,
            relation_id=relation,
            source_ref_id=link["source_ref_id"],
            field_path=link["field_path"],
            observed_value=link["observed_value"],
        )
        if previous and digest(
            {k: str(v) if isinstance(v, UUID) else v for k, v in previous.items()}
        ) != digest(values):
            raise DomainError(422, "Правка свидетельства требует нового ID")
        cur.execute(
            "INSERT INTO "
            "source_links(id,program_id,entity_id,relation_id,source_ref_id,field_path,"
            "observed_value) "
            "VALUES(%s,%s,%s,%s,%s,%s,%s) ON CONFLICT(id) DO NOTHING",
            (
                fact_id,
                program_id,
                entity,
                relation,
                link["source_ref_id"],
                link["field_path"],
                Jsonb(link["observed_value"]),
            ),
        )
        cur.execute(
            "INSERT INTO graph_source_links VALUES(%s,%s,%s,%s,%s,%s,%s,%s)",
            (
                program_id,
                state,
                fact_id,
                entity,
                relation,
                link["source_ref_id"],
                position,
                link["id"],
            ),
        )
    assembled = load_graph(cur, state, data.get("review"))
    if digest(assembled) != digest(data):
        raise ValueError("Граф не восстанавливает исходный контракт без потерь")
    cur.execute(
        "UPDATE graph_states SET status='sealed',content_hash=%s,sealed_at=now() WHERE id=%s",
        (graph_hash, state),
    )
    return state


def journal(cur, program_id, version_id, before, after, *, actor_type="system", actor_id="backend"):
    changes = []

    def equivalent(old, new):
        """Сравнивает JSON со стороны PostgreSQL, включая равенство чисел."""
        if isinstance(old, bool) or isinstance(new, bool):
            return type(old) is type(new) and old == new
        if isinstance(old, (int, float)) and isinstance(new, (int, float)):
            return old == new
        if isinstance(old, dict) and isinstance(new, dict):
            return old.keys() == new.keys() and all(equivalent(old[k], new[k]) for k in old)
        if isinstance(old, list) and isinstance(new, list):
            return len(old) == len(new) and all(
                equivalent(a, b) for a, b in zip(old, new, strict=True)
            )
        return type(old) is type(new) and old == new

    def diff(kind, identity, old, new, path=""):
        if equivalent(old, new):
            return
        if isinstance(old, dict) and isinstance(new, dict):
            for key in sorted(old.keys() | new.keys()):
                pointer = path + "/" + key.replace("~", "~0").replace("/", "~1")
                if key not in old:
                    changes.append((kind, identity, pointer, "add", None, Jsonb(new[key])))
                elif key not in new:
                    changes.append((kind, identity, pointer, "remove", Jsonb(old[key]), None))
                else:
                    diff(kind, identity, old[key], new[key], pointer)
        else:
            changes.append((kind, identity, path, "replace", Jsonb(old), Jsonb(new)))

    for kind, key in (("entity", "entities"), ("relation", "relations")):
        old = {item["id"]: item for item in before.get(key, [])}
        new = {item["id"]: item for item in after.get(key, [])}
        for identity in sorted(old.keys() | new.keys()):
            if identity not in old:
                changes.append((kind, identity, "", "add", None, Jsonb(new[identity])))
            elif identity not in new:
                changes.append((kind, identity, "", "remove", Jsonb(old[identity]), None))
            else:
                diff(kind, identity, old[identity], new[identity])
    for key in ("entities", "relations"):
        old_order = [item["id"] for item in before.get(key, [])]
        new_order = [item["id"] for item in after.get(key, [])]
        if old_order != new_order and set(old_order) == set(new_order):
            changes.append(
                (
                    "graph",
                    str(program_id),
                    f"/{key}/order",
                    "replace",
                    Jsonb(old_order),
                    Jsonb(new_order),
                )
            )
    for key in ("metadata", "document_ids", "source_refs", "source_links", "review"):
        diff(
            "review" if key == "review" else "graph",
            str(program_id),
            before.get(key),
            after.get(key),
            "/" + key,
        )
    for position, (kind, identity, path, action, old, new) in enumerate(changes):
        cur.execute(
            "INSERT INTO "
            "change_log(id,program_id,version_id,position,target_kind,target_id,field_path,"
            "action,old_value,new_value,actor_type,actor_id) "
            "VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
            (
                uuid4(),
                program_id,
                version_id,
                position,
                kind,
                identity,
                path,
                action,
                old,
                new,
                actor_type,
                actor_id,
            ),
        )
