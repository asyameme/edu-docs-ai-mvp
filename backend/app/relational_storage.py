"""Запись предметных объектов в той же транзакции, что и снимок версии."""

from uuid import UUID, uuid5

from psycopg.types.json import Jsonb

from app.errors import DomainError


def insert_objects(cur, version_id, snapshot):
    for entity in snapshot.entities:
        cur.execute(
            "INSERT INTO entities(version_id,id,kind,attributes) VALUES(%s,%s,%s,%s)",
            (version_id, entity.id, entity.kind, Jsonb(entity.attributes)),
        )
    for relation in snapshot.relations:
        attributes = getattr(relation, "attributes", {})
        cur.execute(
            """INSERT INTO relations(version_id,id,source_entity_id,target_entity_id,
               type,matrix_value,attributes) VALUES(%s,%s,%s,%s,%s,%s,%s)""",
            (
                version_id,
                relation.id,
                relation.source_id,
                relation.target_id,
                relation.type,
                attributes.get("matrix_value"),
                Jsonb(attributes),
            ),
        )


def insert_projection(cur, version_id, snapshot):
    insert_objects(cur, version_id, snapshot)
    refs = {ref.id: ref.id for ref in snapshot.source_refs}
    for ref in snapshot.source_refs:
        if getattr(ref, "artifact_id", None) and not cur.execute(
            "SELECT id FROM document_artifacts WHERE document_id=%s AND id=%s",
            (ref.document_id, ref.artifact_id),
        ).fetchone():
            raise DomainError(422, "Markdown не принадлежит документу источника")
        cur.execute(
            """INSERT INTO source_refs(id,version_id,document_id,artifact_id,
               section,locator,artifact_locator) VALUES(%s,%s,%s,%s,%s,%s,%s)""",
            (
                refs[ref.id],
                version_id,
                ref.document_id,
                getattr(ref, "artifact_id", None),
                ref.section,
                ref.locator,
                getattr(ref, "artifact_locator", None),
            ),
        )
    links = getattr(snapshot, "source_links", None)
    if links is not None:
        for link in links:
            insert_link(
                cur,
                version_id,
                link.id,
                link.target_kind,
                link.target_id,
                link.field_path,
                refs[link.source_ref_id],
                link.observed_value,
            )
    covered = {(link.target_kind, link.target_id, link.source_ref_id) for link in links or []}
    for kind, items in (("entity", snapshot.entities), ("relation", snapshot.relations)):
        for item in items:
            for ref_id in set(item.source_ref_ids):
                if (kind, item.id, ref_id) in covered:
                    continue
                identity = uuid5(UUID(str(version_id)), f"{kind}:{item.id}:{ref_id}")
                insert_link(
                    cur, version_id, identity, kind, item.id, "$", refs[ref_id], {"value": None}
                )


def insert_link(cur, version_id, identity, kind, target, path, ref, observed):
    cur.execute(
        """INSERT INTO source_links(id,version_id,entity_id,relation_id,field_path,
           source_ref_id,observed_value) VALUES(%s,%s,%s,%s,%s,%s,%s)""",
        (
            identity,
            version_id,
            target if kind == "entity" else None,
            target if kind == "relation" else None,
            path,
            ref,
            Jsonb(observed),
        ),
    )
