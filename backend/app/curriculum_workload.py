"""Связь графа нагрузки учебного плана с исходными ячейками DOCX."""

import json
from uuid import UUID, uuid5

from app.domain import SourceLink, SourceRefV2

HOURS_COMPONENT_PREFIX = "/attributes/hours_by_kind/"
HOURS_TOTAL_PATH = "/attributes/hours_total"


def workload_evidence(program_id, entity_id, document_id, cells, *, source=None):
    for name, cell in cells.items():
        path = HOURS_COMPONENT_PREFIX + name.replace("~", "~0").replace("/", "~1")
        locator = json.dumps(
            cell["source_ref"], sort_keys=True, ensure_ascii=False, separators=(",", ":")
        )
        ref = SourceRefV2(
            id=uuid5(program_id, f"source:{document_id}:{locator}"),
            document_id=document_id,
            section=cell["source_ref"].get("section"),
            locator=locator,
            artifact_id=source.artifact_id if source else None,
            artifact_locator=source.artifact_locator if source else None,
        )
        yield ref, SourceLink(
            id=uuid5(program_id, f"link:entity:{entity_id}:{path}:{ref.id}"),
            target_kind="entity",
            target_id=entity_id,
            field_path=path,
            source_ref_id=ref.id,
            observed_value={"value": cell["hours"], "unit": None, "cell_text": cell["text"]},
        )


def with_legacy_curriculum_workload(snapshot, program_id, entity_ids, read_document):
    """Подключить ячейки из неизменяемого оригинала для ранее разобранных строк."""
    wanted = {
        item.id: item for item in snapshot.entities
        if item.id in entity_ids and item.kind == "curriculum_item"
        and "hours_by_kind" not in item.attributes
    }
    if not wanted:
        return snapshot
    from worker.parser.parse_curriculum import parse_curriculum_content

    refs = {ref.id: ref for ref in snapshot.source_refs}
    parsed = {}
    members = {}
    links = []
    for link in snapshot.source_links:
        entity = wanted.get(link.target_id)
        if link.target_kind != "entity" or entity is None or link.field_path != HOURS_TOTAL_PATH:
            links.append(link)
            continue
        ref = refs[link.source_ref_id]
        if ref.document_id not in parsed:
            document = read_document(program_id, ref.document_id)
            try:
                plan = parse_curriculum_content(document["content"], document["original_name"])
            except ValueError:
                parsed[ref.document_id] = {}
            else:
                parsed[ref.document_id] = {
                    (row.source_ref.table, row.source_ref.row): row for row in plan.curriculum_items
                }
        locator = json.loads(ref.locator)
        row = parsed[ref.document_id].get((locator.get("table"), locator.get("row")))
        if (
            not row or not row.workload_cells or row.hours_total != entity.attributes["hours_total"]
            or row.semester != entity.attributes.get("semester")
        ):
            links.append(link)
            continue
        links.append(link.model_copy(update={
            "observed_value": {**link.observed_value, "calculated": True},
        }))
        cells = {
            name: cell.model_dump(mode="json") for name, cell in row.workload_cells.items()
        }
        pairs = list(
            workload_evidence(UUID(str(program_id)), entity.id, ref.document_id, cells, source=ref)
        )
        for new_ref, evidence in pairs:
            refs.setdefault(new_ref.id, new_ref)
            links.append(evidence)
        hours_by_kind = {
            name: cell.hours for name, cell in row.workload_cells.items()
        }
        source_ref_ids = list(
            dict.fromkeys([*entity.source_ref_ids, *(item.id for item, _ in pairs)])
        )
        members[entity.id] = entity.model_copy(update={
            "attributes": {**entity.attributes, "hours_by_kind": hours_by_kind},
            "source_ref_ids": source_ref_ids,
        })
    if not members:
        return snapshot
    return snapshot.model_copy(update={
        "entities": [members.get(item.id, item) for item in snapshot.entities],
        "source_refs": list(refs.values()),
        "source_links": links,
    })
