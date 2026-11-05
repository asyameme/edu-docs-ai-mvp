"""Перенос наблюдений документов V2 в версионные узлы, связи и источники.

Объекты разных документов не склеиваются по одному названию. Сопоставление
и выбор между противоречащими наблюдениями остаются отдельным этапом анализа.
"""

import json
from collections import defaultdict
from copy import deepcopy
from uuid import UUID, uuid5

from app.domain import SnapshotV2
from app.parser_document import parse_output

GROUPS = {
    "disciplines": ("discipline", "name"),
    "competencies": ("competency", "code"),
    "indicators": ("indicator", "code"),
    "learning_outcomes": ("learning_outcome", "local_id"),
    "assessment_tools": ("assessment_tool", "name"),
    "curriculum_items": ("curriculum_item", "discipline_name"),
    "course_content": ("course_content", "topic"),
    "score_scale": ("score_scale", "grade_spbu"),
    "document_sections": ("document_section", "section_number"),
    "semester_workload": ("semester_load", "semester"),
    "practice_stages": ("practice_stage", "name"),
    "professional_roles": ("professional_role", "local_id"),
}


def canonical(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"))


class DocumentBuilder:
    def __init__(self, program_id):
        self.program_id = UUID(str(program_id))
        self.entities, self.relations, self.refs, self.links = {}, [], {}, {}
        self.local, self.global_keys, self.occurrences = (
            defaultdict(list),
            defaultdict(list),
            defaultdict(int),
        )
        self.issues, self.unresolved = [], []

    def identity(self, value):
        return str(uuid5(self.program_id, value))

    def source(self, document_id, ref):
        locator = canonical(ref)
        identity = self.identity(f"source:{document_id}:{locator}")
        self.refs[identity] = {
            "id": identity,
            "document_id": document_id,
            "section": ref.get("section"),
            "locator": locator,
        }
        return identity

    def evidence(self, item, target_kind, document_id, ref, attributes):
        ref_id = self.source(document_id, ref)
        if ref_id not in item["source_ref_ids"]:
            item["source_ref_ids"].append(ref_id)
        for key, value in attributes.items():
            pointer = "/attributes/" + key.replace("~", "~0").replace("/", "~1")
            identity = self.identity(f"link:{target_kind}:{item['id']}:{pointer}:{ref_id}")
            self.links[identity] = {
                "id": identity,
                "target_kind": target_kind,
                "target_id": item["id"],
                "field_path": pointer,
                "source_ref_id": ref_id,
                "observed_value": {"value": deepcopy(value)},
            }

    def register(self, document_id, kind, key, identity):
        for mapping, index in (
            (self.local, (document_id, kind, str(key))),
            (self.global_keys, (kind, str(key))),
        ):
            if identity not in mapping[index]:
                mapping[index].append(identity)

    def entity(self, document_id, kind, key, member, *, placeholder=False):
        ref = member["source_ref"]
        seed = f"entity:{document_id}:{kind}:{key}"
        if not placeholder:
            seed += ":" + canonical(ref)
            ordinal = self.occurrences[seed]
            self.occurrences[seed] += 1
            seed += f":{ordinal}"
        identity = self.identity(seed)
        attributes = {k: deepcopy(v) for k, v in member.items() if k != "source_ref"}
        item = self.entities.setdefault(
            identity,
            {
                "id": identity,
                "kind": kind,
                "attributes": attributes,
                "source_ref_ids": [],
            },
        )
        self.evidence(item, "entity", document_id, ref, attributes)
        self.register(document_id, kind, key, identity)
        if kind == "curriculum_item":
            self.register(document_id, kind, f"{key}::semester={attributes['semester']}", identity)
        return identity

    def resolve(self, document_id, kind, key, ref, *, create=False):
        kind = {"course_topic": "course_content", "semester_workload": "semester_load"}.get(
            kind, kind
        )
        candidates = self.local[document_id, kind, str(key)]
        # Ячейка матрицы сама свидетельствует о своих концах связи. Не выбираем
        # за пользователя одно из одноимённых наблюдений в других документах.
        if not candidates and not create:
            candidates = self.global_keys[kind, str(key)]
        if len(candidates) > 1 and ref.get("table") is not None and ref.get("row") is not None:
            candidates = [
                identity
                for identity in candidates
                if any(
                    all(
                        json.loads(self.refs[r]["locator"]).get(k) == ref.get(k)
                        for k in ("file", "table", "row")
                    )
                    for r in self.entities[identity]["source_ref_ids"]
                )
            ]
        if len(candidates) == 1:
            return candidates[0]
        if not candidates and create:
            field = "code" if kind == "competency" else "name"
            return self.entity(
                document_id, kind, key, {field: key, "source_ref": ref}, placeholder=True
            )
        return None

    def relation(self, document_id, source, target, kind, ref, attributes=None):
        attributes = deepcopy(attributes or {})
        kind = "FORMS_COMPETENCY" if kind == "формирует" else kind
        seed = f"relation:{document_id}:{source}:{target}:{kind}:{canonical(ref)}"
        ordinal = self.occurrences[seed]
        self.occurrences[seed] += 1
        item = {
            "id": self.identity(f"{seed}:{ordinal}"),
            "source_id": source,
            "target_id": target,
            "type": kind,
            "attributes": attributes,
            "source_ref_ids": [],
        }
        self.evidence(item, "relation", document_id, ref, attributes)
        if not attributes:
            ref_id = self.source(document_id, ref)
            identity = self.identity(f"link:relation:{item['id']}:$:{ref_id}")
            self.links[identity] = {
                "id": identity,
                "target_kind": "relation",
                "target_id": item["id"],
                "field_path": "$",
                "source_ref_id": ref_id,
                "observed_value": {"value": None},
            }
        self.relations.append(item)

    def connect(
        self,
        document_id,
        member,
        source_kind,
        source_key,
        target_kind,
        target_key,
        relation_type,
        attributes=None,
        *,
        create=False,
    ):
        ref = member["source_ref"]
        source = self.resolve(document_id, source_kind, source_key, ref, create=create)
        target = self.resolve(document_id, target_kind, target_key, ref, create=create)
        if source is None or target is None:
            self.unresolved.append({"document_id": document_id, **deepcopy(member)})
            self.issues.append(
                {
                    "document_id": document_id,
                    "category": "unresolved_relation",
                    "severity": "warning",
                    "message": "Связь отсутствует или неоднозначна",
                }
            )
            return
        self.relation(document_id, source, target, relation_type, ref, attributes)


def to_snapshot_v2(program_id, files):
    builder, parsed_documents, codes = DocumentBuilder(program_id), {}, {}
    for item in files:
        output = item["output"]
        if not output:
            continue
        parsed = parse_output(output)
        document_id = str(item["document_id"])
        # Проверенный оригинальный JSON сохраняется целиком, включая диагностику и null.
        parsed_documents[document_id] = deepcopy(output)
        code = parsed.meta.program_id
        if code:
            try:
                internal_id = UUID(code)
            except ValueError:
                codes[document_id] = code
            else:
                if internal_id != builder.program_id:
                    raise ValueError("Результат документа принадлежит другой программе")
        groups = dict(GROUPS)
        if output.get("program"):
            groups["program"] = ("program", "name")
        for group, (kind, key_field) in groups.items():
            members = [output[group]] if group == "program" else output.get(group, [])
            for member in members:
                identity = builder.entity(document_id, kind, member.get(key_field) or kind, member)
                if kind == "program" and code:
                    builder.register(document_id, kind, code, identity)
        builder.issues.extend(
            {**deepcopy(issue), "document_id": document_id}
            for issue in output.get("parse_issues", [])
        )

    for document_id, output in parsed_documents.items():
        for member in output.get("relations", []):
            builder.connect(
                document_id,
                member,
                member["src_entity"],
                member["src_key"],
                member["dst_entity"],
                member["dst_key"],
                member["kind"],
                {k: v for k, v in member.items() if k != "source_ref"},
            )
        for cell in output.get("matrix_cells", []):
            builder.connect(
                document_id,
                cell,
                "discipline",
                cell["discipline_name"],
                "competency",
                cell["competency_code"],
                "FORMS_COMPETENCY",
                {"matrix_value": cell["value"], "semester": cell.get("semester")},
                create=True,
            )
        for member in output.get("role_competency_links", []):
            builder.connect(
                document_id,
                member,
                "professional_role",
                member["role_id"],
                "competency",
                member["competency_code"],
                "ROLE_COMPETENCY",
            )
        for member in output.get("indicator_outcome_links", []):
            builder.connect(
                document_id,
                member,
                "indicator",
                member["indicator_code"],
                "learning_outcome",
                member["learning_outcome_id"],
                "INDICATOR_OUTCOME",
            )
        disciplines = (
            builder.local[
                document_id, "discipline", output.get("disciplines", [{}])[0].get("name", "")
            ]
            if output.get("disciplines")
            else []
        )
        if len(output.get("disciplines", [])) == 1 and len(disciplines) == 1:
            for kind in (
                "semester_load",
                "practice_stage",
                "document_section",
                "course_content",
                "score_scale",
            ):
                for entity in list(builder.entities.values()):
                    if entity["kind"] == kind and any(
                        builder.refs[r]["document_id"] == document_id
                        for r in entity["source_ref_ids"]
                    ):
                        ref = json.loads(builder.refs[entity["source_ref_ids"][0]]["locator"])
                        builder.relation(
                            document_id, disciplines[0], entity["id"], f"HAS_{kind.upper()}", ref
                        )
    snapshot = SnapshotV2.model_validate(
        {
            "entities": list(builder.entities.values()),
            "relations": builder.relations,
            "source_refs": list(builder.refs.values()),
            "source_links": list(builder.links.values()),
            "document_ids": [str(item["document_id"]) for item in files],
            "metadata": {
                "program_id": str(builder.program_id),
                "parsed_documents": parsed_documents,
                "document_program_codes": codes,
                "unresolved_relations": builder.unresolved,
                "parse_issues": builder.issues,
            },
        }
    )
    return snapshot.model_dump(mode="json"), builder.issues
