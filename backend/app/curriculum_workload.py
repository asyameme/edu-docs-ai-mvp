"""Связь нагрузки в графе с исходными ячейками DOCX."""

import json
from collections import defaultdict
from io import BytesIO
from uuid import UUID, uuid5
from zipfile import BadZipFile

from docx import Document

from app.domain import SourceLink, SourceRefV2

HOURS_BY_KIND_PATH = "/attributes/hours_by_kind"
HOURS_COMPONENT_PREFIX = HOURS_BY_KIND_PATH + "/"
HOURS_TOTAL_PATH = "/attributes/hours_total"
WORKLOAD_ENTITY_KINDS = {"curriculum_item", "discipline"}


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
            observed_value={
                "value": cell["hours"], "unit": None, "cell_text": cell["text"],
                **({"semester_hours": cell["semester_hours"]} if "semester_hours" in cell else {}),
                **({"workload_mirror": True} if cell.get("workload_mirror") else {}),
            },
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


def _rpd_workload_rows(source):
    from worker.parser.discipline import extract_workload, organization_variant_rows

    document = Document(BytesIO(source["content"]))
    name = source["original_name"]
    values, _ = extract_workload(document, name)
    profiles, _ = organization_variant_rows(document, name)
    rows = defaultdict(list)
    for item in values:
        ref = item["source_ref"]
        if item["kind"] in {"hours", "hours_total"}:
            rows[(ref.table, ref.row)].append(item)
    return rows, profiles


def _rpd_cells(items, period):
    from worker.parser.common import normalize
    from worker.parser.discipline import workload_hour_label

    cells = {}
    for item in items:
        if item["kind"] != "hours" or {normalize(p) for p in item["periods"]} != {period}:
            return {}
        label = workload_hour_label(item)
        value = item["value"]
        if label is None or label in cells or value is None or value < 0 or value % 1:
            return {}
        cells[label] = {
            "hours": int(value),
            "text": item["raw"],
            "source_ref": item["source_ref"].model_dump(mode="json"),
        }
    return cells


def _discipline_workload_rows(entity, ref, rows, profiles):
    """Вернуть ячейки односеместрового профиля и соответствующей строки «Итого»."""
    locator = json.loads(ref.locator)
    position = (locator.get("table"), locator.get("row"))
    period = f"семестр {entity.attributes['semesters'][0]}"
    primary = semester_row = _rpd_cells(rows.get(position, []), period)
    if not primary:
        for total in ("итого", "всего"):
            primary = _rpd_cells(rows.get(position, []), total)
            if primary:
                break
    observed = {name: cell["hours"] for name, cell in primary.items()}
    if not primary or observed != entity.attributes.get("hours_by_kind"):
        return None
    if sum(observed.values()) != entity.attributes.get("hours_total"):
        return None
    if semester_row:
        return primary, {}

    candidates = [
        _rpd_cells(items, period)
        for other, items in rows.items()
        if other != position and other[0] == position[0]
        and profiles.get(other) == profiles.get(position)
    ]
    candidates = [cells for cells in candidates if cells]
    if len(candidates) != 1 or candidates[0].keys() != primary.keys():
        return None
    semester = candidates[0]
    for name, cell in primary.items():
        cell["semester_hours"] = semester[name]["hours"]
        semester[name]["workload_mirror"] = True
    return primary, semester


def with_editable_workload(snapshot, program_id, entity_ids, read_document):
    snapshot = with_legacy_curriculum_workload(snapshot, program_id, entity_ids, read_document)
    prepared = {
        link.target_id for link in snapshot.source_links
        if link.field_path.startswith(HOURS_COMPONENT_PREFIX)
    }
    disciplines = {
        item.id: item for item in snapshot.entities
        if item.id in entity_ids and item.id not in prepared and item.kind == "discipline"
        and len(item.attributes.get("semesters") or []) == 1
        and type(item.attributes.get("hours_total")) is int
    }
    if not disciplines:
        return snapshot

    refs = {ref.id: ref for ref in snapshot.source_refs}
    parsed = {}
    members = {}
    cell_links = []
    calculated_links = set()
    for link in snapshot.source_links:
        entity = disciplines.get(link.target_id)
        if link.target_kind != "entity" or entity is None or link.field_path != HOURS_TOTAL_PATH:
            continue
        ref = refs[link.source_ref_id]
        if ref.document_id not in parsed:
            source = read_document(program_id, ref.document_id)
            try:
                parsed[ref.document_id] = _rpd_workload_rows(source)
            except BadZipFile:
                parsed[ref.document_id] = None
        source_rows = parsed[ref.document_id]
        if source_rows is None:
            continue
        cell_rows = _discipline_workload_rows(entity, ref, *source_rows)
        if cell_rows is None:
            continue
        source_ref_ids = list(entity.source_ref_ids)
        for cells in cell_rows:
            for cell_ref, evidence in workload_evidence(
                UUID(str(program_id)), entity.id, ref.document_id, cells, source=ref,
            ):
                refs.setdefault(cell_ref.id, cell_ref)
                source_ref_ids.append(cell_ref.id)
                cell_links.append(evidence)
        members[entity.id] = entity.model_copy(update={
            "source_ref_ids": list(dict.fromkeys(source_ref_ids)),
        })
        calculated_links.add((entity.id, ref.id))
    if not members:
        return snapshot
    links = [
        link.model_copy(update={"observed_value": {**link.observed_value, "calculated": True}})
        if (link.target_id, link.source_ref_id) in calculated_links
        and link.target_kind == "entity"
        and link.field_path in {HOURS_TOTAL_PATH, HOURS_BY_KIND_PATH}
        else link
        for link in snapshot.source_links
    ]
    return snapshot.model_copy(update={
        "entities": [members.get(item.id, item) for item in snapshot.entities],
        "source_refs": list(refs.values()),
        "source_links": links + cell_links,
    })
