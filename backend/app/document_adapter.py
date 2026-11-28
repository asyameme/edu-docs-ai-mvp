"""Перенос наблюдений документов V2 в версионные узлы, связи и источники.

Объекты разных документов не склеиваются по одному названию. Сопоставление
и выбор между противоречащими наблюдениями остаются отдельным этапом анализа.
"""

import json
from collections import defaultdict
from copy import deepcopy
from uuid import UUID, uuid5

from app.curriculum_workload import HOURS_TOTAL_PATH, workload_evidence
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

    def evidence(self, item, target_kind, document_id, ref, attributes, observed_attributes=None):
        ref_id = self.source(document_id, ref)
        if ref_id not in item["source_ref_ids"]:
            item["source_ref_ids"].append(ref_id)
        observed_attributes = attributes if observed_attributes is None else observed_attributes
        for key in attributes:
            if key not in observed_attributes:
                raise ValueError(f"Исходное наблюдение поля {key} не найдено")
            pointer = "/attributes/" + key.replace("~", "~0").replace("/", "~1")
            identity = self.identity(f"link:{target_kind}:{item['id']}:{pointer}:{ref_id}")
            self.links[identity] = {
                "id": identity,
                "target_kind": target_kind,
                "target_id": item["id"],
                "field_path": pointer,
                "source_ref_id": ref_id,
                "observed_value": {"value": deepcopy(observed_attributes[key])},
            }

    def register(self, document_id, kind, key, identity):
        for mapping, index in (
            (self.local, (document_id, kind, str(key))),
            (self.global_keys, (kind, str(key))),
        ):
            if identity not in mapping[index]:
                mapping[index].append(identity)

    def entity(self, document_id, kind, key, member, *, placeholder=False, original_member=None):
        ref = member["source_ref"]
        seed = f"entity:{document_id}:{kind}:{key}"
        if not placeholder:
            seed += ":" + canonical(ref)
            ordinal = self.occurrences[seed]
            self.occurrences[seed] += 1
            seed += f":{ordinal}"
        identity = self.identity(seed)
        attributes = {k: deepcopy(v) for k, v in member.items() if k != "source_ref"}
        workload = attributes.pop("workload_cells", None)
        if workload:
            attributes["hours_by_kind"] = {name: cell["hours"] for name, cell in workload.items()}
        item = self.entities.setdefault(
            identity,
            {
                "id": identity,
                "kind": kind,
                "attributes": attributes,
                "source_ref_ids": [],
            },
        )
        original_member = member if original_member is None else original_member
        observed = {k: deepcopy(v) for k, v in original_member.items() if k != "source_ref"}
        fields = {
            key: value for key, value in attributes.items()
            if not workload or key != "hours_by_kind"
        }
        self.evidence(item, "entity", document_id, ref, fields, observed)
        if workload:
            ref_id = self.source(document_id, ref)
            total_link = self.identity(f"link:entity:{identity}:{HOURS_TOTAL_PATH}:{ref_id}")
            self.links[total_link]["observed_value"]["calculated"] = True
            for cell_ref, cell_link in workload_evidence(
                self.program_id, identity, document_id, workload,
            ):
                ref_id, link_id = str(cell_ref.id), str(cell_link.id)
                self.refs.setdefault(ref_id, cell_ref.model_dump(mode="json"))
                self.links[link_id] = cell_link.model_dump(mode="json")
                if ref_id not in item["source_ref_ids"]:
                    item["source_ref_ids"].append(ref_id)
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
            exact_location = [
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
            if exact_location:
                candidates = exact_location
        if len(candidates) > 1 and ref.get("section") is not None:
            section_location = [
                identity
                for identity in candidates
                if any(
                    all(
                        json.loads(self.refs[r]["locator"]).get(k) == ref.get(k)
                        for k in ("file", "table", "section")
                        if ref.get(k) is not None
                    )
                    for r in self.entities[identity]["source_ref_ids"]
                )
            ]
            if section_location:
                candidates = section_location
        if len(candidates) == 1:
            return candidates[0]
        if not candidates and create:
            field = "code" if kind == "competency" else "name"
            return self.entity(
                document_id, kind, key, {field: key, "source_ref": ref}, placeholder=True
            )
        return None

    def relation(
        self, document_id, source, target, kind, ref, attributes=None, observed_attributes=None
    ):
        attributes = deepcopy(attributes or {})
        observed_attributes = attributes if observed_attributes is None else observed_attributes
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
        self.evidence(item, "relation", document_id, ref, attributes, observed_attributes)
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
        original_member=None,
        shared_discipline=False,
    ):
        ref = member["source_ref"]
        if shared_discipline:
            sources = self.local[document_id, source_kind, str(source_key)]
        else:
            sources = [self.resolve(document_id, source_kind, source_key, ref, create=create)]
        target = self.resolve(document_id, target_kind, target_key, ref, create=create)
        if not sources or sources[0] is None or target is None:
            def candidates(kind, key):
                local = self.local[document_id, kind, str(key)]
                return list(local or self.global_keys[kind, str(key)])

            self.unresolved.append({
                "document_id": document_id, **deepcopy(member),
                "source_ref_id": self.source(document_id, ref),
                "resolution": {
                    "source_kind": source_kind, "source_key": str(source_key),
                    "target_kind": target_kind, "target_key": str(target_key),
                    "source_candidates": candidates(source_kind, source_key),
                    "target_candidates": candidates(target_kind, target_key),
                },
            })
            self.issues.append(
                {
                    "document_id": document_id,
                    "category": "unresolved_relation",
                    "severity": "warning",
                    "message": "Связь отсутствует или неоднозначна",
                }
            )
            return
        observed_attributes = (
            {k: v for k, v in original_member.items() if k != "source_ref"}
            if original_member is not None
            else attributes
        )
        for source in sources:
            self.relation(
                document_id, source, target, relation_type, ref, attributes, observed_attributes
            )


def to_snapshot_v2(
    program_id, files, *, original_files=None, relation_origins=None, return_model=False
):
    builder, parsed_documents, codes = DocumentBuilder(program_id), {}, {}
    normalized_documents = {}
    store_selected_json = original_files is None
    originals, original_fingerprints, original_program_codes = {}, {}, {}
    for item in original_files if original_files is not None else files:
        raw = item.get("output", item.get("parsed"))
        if not raw:
            continue
        parsed = parse_output(raw)
        document_id = str(item["document_id"])
        originals[document_id] = parsed.model_dump(mode="json", exclude_none=False)
        original_fingerprints[document_id] = canonical(
            raw.model_dump(mode="json", exclude_none=False)
            if hasattr(raw, "model_dump") else raw
        )
        original_program_codes[document_id] = parsed.meta.program_id
    for item in files:
        raw_output = item["output"]
        if not raw_output:
            continue
        document_id = str(item["document_id"])
        output_fingerprint = canonical(
            raw_output.model_dump(mode="json", exclude_none=False)
            if hasattr(raw_output, "model_dump") else raw_output
        )
        if output_fingerprint == original_fingerprints.get(document_id):
            output = originals[document_id]
            code = original_program_codes[document_id]
        else:
            parsed = parse_output(raw_output)
            output = parsed.model_dump(mode="json", exclude_none=False)
            code = parsed.meta.program_id
        original = originals.get(document_id)
        if original is None:
            raise ValueError("Исходный JSON документа отсутствует")
        # Проверенный оригинальный JSON сохраняется целиком, включая диагностику и null.
        if store_selected_json:
            parsed_documents[document_id] = deepcopy(raw_output)
        normalized_documents[document_id] = output
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
            original_members = [original[group]] if group == "program" else original.get(group, [])
            if len(members) != len(original_members):
                raise ValueError(f"Выбор изменил структуру раздела {group}")
            for member, original_member in zip(members, original_members, strict=True):
                if member.get("source_ref") != original_member.get("source_ref"):
                    raise ValueError("Выбор изменил provenance исходной записи")
                identity = builder.entity(
                    document_id,
                    kind,
                    member.get(key_field) or kind,
                    member,
                    original_member=original_member,
                )
                if kind == "program" and code:
                    builder.register(document_id, kind, code, identity)
        builder.issues.extend(
            {**deepcopy(issue), "document_id": document_id}
            for issue in output.get("parse_issues", [])
        )

    for document_id, output in normalized_documents.items():
        original_relations = originals[document_id].get("relations", [])
        discipline_names = {member["name"] for member in output.get("disciplines", [])}
        # В РПД с несколькими профилями таблица результатов общая.
        # Нагрузка хранится отдельно для каждого профиля, а компетенции относятся к обоим.
        multi_profile_rpd = (
            output["meta"]["doc_type"] == "RPD"
            and len(output["disciplines"]) > 1
            and len(discipline_names) == 1
        )
        selected_relations = output.get("relations", [])
        origins = (relation_origins or {}).get(document_id, list(range(len(original_relations))))
        if len(selected_relations) != len(origins):
            raise ValueError("Выбор изменил структуру списка связей")
        for member, origin in zip(selected_relations, origins, strict=True):
            original_member = original_relations[origin] if origin is not None else None
            if (
                original_member is not None
                and member.get("source_ref") != original_member.get("source_ref")
            ):
                raise ValueError("Выбор изменил provenance исходной связи")
            for endpoint_kind, endpoint_key in (
                (member["src_entity"], member["src_key"]),
                (member["dst_entity"], member["dst_key"]),
            ):
                # РПД может ссылаться на компетенцию одним кодом без локальной
                # формулировки (описание живет в ЦКМВ или учебном плане). Глобальный
                # поиск в этот момент находит несколько одноименных сущностей других
                # документов и не выбирает одну. Код известен программе, поэтому
                # фиксируем ссылку местной записью; неизвестный программе код
                # остается неразрешенным и видимым как раньше.
                if (
                    endpoint_kind == "competency"
                    and not builder.local[document_id, "competency", str(endpoint_key)]
                    and builder.global_keys["competency", str(endpoint_key)]
                ):
                    builder.entity(
                        document_id,
                        "competency",
                        endpoint_key,
                        {"code": endpoint_key, "source_ref": member["source_ref"]},
                        placeholder=True,
                    )
            # A user-added edge is anchored to the row but was not observed in the DOCX.
            attributes = (
                {k: v for k, v in member.items() if k != "source_ref"}
                if original_member is not None else {}
            )
            builder.connect(
                document_id,
                member,
                member["src_entity"],
                member["src_key"],
                member["dst_entity"],
                member["dst_key"],
                member["kind"],
                attributes,
                original_member=original_member,
                shared_discipline=(
                    multi_profile_rpd
                    and member["src_entity"] == "discipline"
                    and member["dst_entity"] == "competency"
                    and member["kind"] == "формирует"
                    and member["src_key"] in discipline_names
                ),
            )
        matrix_cells = output.get("matrix_cells", [])
        original_matrix_cells = originals[document_id].get("matrix_cells", [])
        if len(matrix_cells) != len(original_matrix_cells):
            raise ValueError("Выбор изменил структуру матрицы связей")
        for cell, original_cell in zip(matrix_cells, original_matrix_cells, strict=True):
            if cell.get("source_ref") != original_cell.get("source_ref"):
                raise ValueError("Выбор изменил provenance ячейки матрицы")
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
                original_member={
                    "source_ref": original_cell["source_ref"],
                    "matrix_value": original_cell["value"],
                    "semester": original_cell.get("semester"),
                },
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
    metadata = {
        "program_id": str(builder.program_id),
        "document_program_codes": codes,
        "unresolved_relations": builder.unresolved,
        "parse_issues": builder.issues,
    }
    if store_selected_json:
        metadata["parsed_documents"] = parsed_documents
    snapshot = SnapshotV2.model_validate(
        {
            "entities": list(builder.entities.values()),
            "relations": builder.relations,
            "source_refs": list(builder.refs.values()),
            "source_links": list(builder.links.values()),
            "document_ids": [str(item["document_id"]) for item in files],
            "metadata": metadata,
        }
    )
    return (snapshot if return_model else snapshot.model_dump(mode="json")), builder.issues
