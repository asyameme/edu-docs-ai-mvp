from __future__ import annotations

import argparse
import json
import math
import sys
from collections import defaultdict
from dataclasses import asdict, dataclass, field
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any, Literal, Mapping, Sequence

if __package__:
    from .indicator_formulations import IndicatorDocument, evaluate_indicator_formulations
    from .text_normalization import (
        normalize_formulation_text,
        normalize_variant_formulation_text,
    )
    from .semantic_llm import SemanticEvaluator
    from .semantic_rpd import evaluate_rpd_alignment
    from .semantic_coverage import CoverageDocument, evaluate_competency_coverage
else:  # Поддерживаем и прямой запуск validator.py.
    from indicator_formulations import IndicatorDocument, evaluate_indicator_formulations
    from text_normalization import (
        normalize_formulation_text,
        normalize_variant_formulation_text,
    )
    from semantic_llm import SemanticEvaluator
    from semantic_rpd import evaluate_rpd_alignment
    from semantic_coverage import CoverageDocument, evaluate_competency_coverage

try:
    import yaml
except ImportError:  # PyYAML устанавливается из backend/requirements.txt
    yaml = None


DEFAULT_RULES = Path(__file__).with_name("rules.yaml")
DOC_TYPES = {"rpd", "rpp", "curriculum", "matrix", "characteristic", "ckmv"}
ValidationPhase = Literal["document_reconciliation", "graph_validation"]
VALIDATION_PHASES = {"document_reconciliation", "graph_validation"}
RULE_STATUSES = {"draft", "ready", "ready_with_semantic_fallback"}
RUNNABLE_RULE_STATUSES = {"ready", "ready_with_semantic_fallback"}


class ValidationError(Exception):
    """Ошибка чтения конфигурации или входных JSON."""


@dataclass(frozen=True)
class Document:
    data: Mapping[str, Any]
    label: str
    document_id: str | None = None

    @property
    def role(self) -> str | None:
        meta = self.data.get("meta", {})
        kind = meta.get("doc_type") if isinstance(meta, Mapping) else None
        role = kind.lower() if isinstance(kind, str) else None
        return role if role in DOC_TYPES else None

    @property
    def program_id(self) -> str | None:
        meta = self.data.get("meta", {})
        value = meta.get("program_id") if isinstance(meta, Mapping) else None
        return value if isinstance(value, str) and value else None


@dataclass
class Finding:
    code: str
    status: str
    message: str
    evidence: dict[str, Any] = field(default_factory=dict)


@dataclass
class RuleResult:
    rule_id: str
    name: str
    status: str
    checks: int
    findings: list[Finding] = field(default_factory=list)


@dataclass
class ValidationReport:
    phase: ValidationPhase
    results: list[RuleResult]

    def to_dict(self) -> dict[str, Any]:
        counts: dict[str, int] = defaultdict(int)
        for result in self.results:
            counts[result.status] += 1
        return {
            "phase": self.phase,
            "summary": {"rules_total": len(self.results), "rules_by_status": dict(counts)},
            "results": [
                {**asdict(result), "findings": [asdict(item) for item in result.findings]}
                for result in self.results
            ],
        }


def load_rules(path: str | Path = DEFAULT_RULES) -> dict[str, Any]:
    if yaml is None:
        raise ValidationError("Установите PyYAML: python -m pip install -r backend/requirements.txt")
    try:
        config = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as exc:
        raise ValidationError(f"Не удалось прочитать rules.yaml: {exc}") from exc
    return _validate_rules_config(config)


def _validate_rules_config(config: Any) -> dict[str, Any]:
    if not isinstance(config, dict) or config.get("schema_version") != 2:
        raise ValidationError("Ожидается rules.yaml со schema_version: 2")
    phases = config.get("phases")
    if not isinstance(phases, dict) or set(phases) != VALIDATION_PHASES:
        raise ValidationError(
            "rules.yaml должен объявлять фазы document_reconciliation и graph_validation"
        )
    expected_inputs = {
        "document_reconciliation": "parsed_documents",
        "graph_validation": "graph_projection",
    }
    for phase, expected_input in expected_inputs.items():
        if not isinstance(phases[phase], dict) or phases[phase].get("input") != expected_input:
            raise ValidationError(
                f"Фаза {phase} должна использовать вход {expected_input}"
            )
    if not isinstance(config.get("rules"), list):
        raise ValidationError("Поле rules в rules.yaml должно быть списком")
    for rule in config["rules"]:
        if not isinstance(rule, dict):
            raise ValidationError("Каждое правило в rules.yaml должно быть объектом")
        if (
            "required_for_confirmation" in rule
            and type(rule["required_for_confirmation"]) is not bool
        ):
            raise ValidationError(
                f"Rule {rule.get('id')}: "
                "required_for_confirmation must be boolean"
            )
        run_mode = rule.get("run_mode", "automatic")
        if run_mode not in ("automatic", "manual"):
            raise ValidationError(
                f"Правило {rule.get('id', '<без id>')} должно иметь run_mode automatic или manual"
            )
        if run_mode == "manual" and rule.get("required_for_confirmation", False):
            raise ValidationError(
                f"Ручное правило {rule.get('id', '<без id>')} не может быть обязательным для подтверждения"
            )
        rule_status = rule.get("status")
        if not isinstance(rule_status, str) or rule_status not in RULE_STATUSES:
            raise ValidationError(
                f"Правило {rule.get('id', '<без id>')} должно содержать один из статусов: "
                f"{', '.join(sorted(RULE_STATUSES))}"
            )
        if rule.get("type") == "credit_hours" and rule_status in RUNNABLE_RULE_STATUSES:
            inputs = rule.get("inputs")
            hours_per_credit_unit = (
                inputs.get("hours_per_credit_unit") if isinstance(inputs, dict) else None
            )
            if (
                isinstance(hours_per_credit_unit, bool)
                or not isinstance(hours_per_credit_unit, (int, float))
                or (
                    isinstance(hours_per_credit_unit, float)
                    and not math.isfinite(hours_per_credit_unit)
                )
                or hours_per_credit_unit <= 0
            ):
                raise ValidationError(
                    f"Для активного правила {rule.get('id', '<без id>')} "
                    "задайте положительное конечное значение inputs.hours_per_credit_unit"
                )
        rule_phases = rule.get("phases")
        if (
            not isinstance(rule_phases, list)
            or not rule_phases
            or any(not isinstance(value, str) for value in rule_phases)
            or len(rule_phases) != len(set(rule_phases))
            or not set(rule_phases).issubset(VALIDATION_PHASES)
        ):
            raise ValidationError(
                f"Правило {rule.get('id', '<без id>')} должно содержать уникальные допустимые phases"
            )
        if run_mode == "manual" and rule_phases != ["graph_validation"]:
            raise ValidationError(
                f"Ручное правило {rule.get('id', '<без id>')} должно запускаться только после сборки графа"
            )
    return config


def _documents(values: Sequence[Mapping[str, Any] | Document]) -> list[Document]:
    result = []
    for index, value in enumerate(values, 1):
        if isinstance(value, Document):
            result.append(value)
            continue
        if not isinstance(value, Mapping):
            raise ValidationError(f"Документ #{index} должен быть JSON-объектом")
        meta = value.get("meta", {})
        label = meta.get("file_name") if isinstance(meta, Mapping) else None
        result.append(Document(value, label if isinstance(label, str) and label else f"document-{index}"))
    return result


def _records(document: Document, path: str) -> list[Mapping[str, Any]]:
    """Поддерживается используемый в конфигурации путь вида $.field[*]."""
    if not path.startswith("$.") or not path.endswith("[*]"):
        raise ValidationError(f"Неподдерживаемый путь в rules.yaml: {path}")
    key = path[2:-3]
    value = document.data.get(key, [])
    if value is None:
        return []
    if not isinstance(value, list) or any(not isinstance(row, Mapping) for row in value):
        raise ValidationError(f"{document.label}: поле {key} должно быть списком объектов")
    return value


def _source_ref(document: Document, row: Mapping[str, Any] | None = None) -> dict[str, Any]:
    ref = row.get("source_ref") if row else None
    if isinstance(ref, Mapping):
        return {"file": document.label, **ref, **({"document_id": document.document_id} if document.document_id else {})}
    return {"file": document.label, **({"document_id": document.document_id} if document.document_id else {})}


def _role_docs(documents: Sequence[Document], role: str) -> list[Document]:
    return [doc for doc in documents if doc.role == role.lower()]


def _document_pairs(
    documents: Sequence[Document], left_role: str, right_role: str
) -> list[tuple[Document, Document]]:
    pairs = []
    for left in _role_docs(documents, left_role):
        for right in _role_docs(documents, right_role):
            if left.program_id and right.program_id and left.program_id != right.program_id:
                continue
            pairs.append((left, right))
    return pairs


def _semesters(row: Mapping[str, Any], field_name: str) -> set[Any]:
    value = row.get(field_name)
    return set(value) if isinstance(value, list) else ({value} if value is not None else set())


def _matched_records(left_doc: Document, right_doc: Document, left: Mapping[str, Any], right: Mapping[str, Any], matching: Mapping[str, Any]):
    pairs = []
    for lrow in _records(left_doc, left["records_path"]):
        for rrow in _records(right_doc, right["records_path"]):
            if matching.get("name") == "equal" and lrow.get(left["name_field"]) != rrow.get(right["name_field"]):
                continue
            common = _semesters(lrow, left["semester_field"]) & _semesters(rrow, right["semester_field"])
            if matching.get("semester") != "right_value_in_left_list":
                raise ValidationError(f"Неизвестное правило сопоставления семестров: {matching.get('semester')}")
            if common:
                pairs.append((lrow, rrow, common))
    return pairs


def _result(rule: Mapping[str, Any], checks: int, findings: list[Finding]) -> RuleResult:
    if checks == 0:
        status = "skipped"
    elif any(item.status == "failed" for item in findings):
        status = "failed"
    elif findings:
        status = "needs_review"
    else:
        status = "passed"
    return RuleResult(str(rule["id"]), str(rule.get("name", rule["id"])), status, checks, findings)


def _sum_hours(rule: Mapping[str, Any], docs: Sequence[Document]) -> RuleResult:
    scope, inputs = rule["scope"], rule["inputs"]
    checks, findings = 0, []
    for doc in docs:
        if doc.role not in scope["document_types"]:
            continue
        for row in _records(doc, scope["records_path"]):
            checks += 1
            total = row.get(inputs["total_hours_field"])
            components = row.get(inputs["components_field"])
            name = row.get("name", "без названия")
            if not _valid_load(total) or not isinstance(components, Mapping) or not components:
                findings.append(Finding("hours_data_missing", "needs_review", f"Нельзя проверить сумму часов для «{name}».", {"source_refs": [_source_ref(doc, row)]}))
                continue
            unknown = {key: value for key, value in components.items()
                       if key not in inputs["include_component_keys"] and key not in inputs.get("exclude_component_keys", [])}
            if unknown:
                findings.append(Finding("unknown_hour_components", "needs_review",
                    f"Для «{name}» обнаружены неизвестные компоненты нагрузки.",
                    {"source_refs": [_source_ref(doc, row)], "unknown_components": unknown}))
                continue
            included = {key: components[key] for key in inputs["include_component_keys"] if key in components}
            if not included or any(not _valid_load(value) for value in included.values()):
                findings.append(Finding("invalid_hour_value", "needs_review", f"Разбивка часов для «{name}» содержит некорректное значение.", {"source_refs": [_source_ref(doc, row)]}))
                continue
            actual = sum(included.values())
            if actual != total:
                excluded = {key: components[key] for key in inputs.get("exclude_component_keys", []) if key in components}
                findings.append(Finding("hours_sum_mismatch", "failed", f"Для «{name}» сумма видов часов {actual}, общий итог {total}.", {
                    "source_refs": [_source_ref(doc, row)], "included_components": included,
                    "excluded_components": excluded, "calculated_hours": actual, "total_hours": total,
                }))
    return _result(rule, checks, findings)


def _compare_fields(rule: Mapping[str, Any], docs: Sequence[Document]) -> RuleResult:
    inputs = rule["inputs"]
    return _compare_workload(rule, docs, inputs["left"]["value_field"], {"rpd"})


def _compare_relations(rule: Mapping[str, Any], docs: Sequence[Document]) -> RuleResult:
    inputs = rule["inputs"]
    left, right = inputs["left"], inputs["right"]
    checks, findings = 0, []
    for ldoc, rdoc in _document_pairs(docs, left["document_type"], right["document_type"]):
        for lrow, rrow, semesters in _matched_records(ldoc, rdoc, left, right, inputs["matching"]):
            checks += 1

            def linked(doc: Document, spec: Mapping[str, Any], row: Mapping[str, Any]) -> list[Mapping[str, Any]]:
                name = row.get(spec["name_field"])
                return [rel for rel in _records(doc, spec["relations_path"])
                        if rel.get("src_key") == name
                        and all(rel.get(key) == value for key, value in spec["relation_filter"].items())]

            lrels, rrels = linked(ldoc, left, lrow), linked(rdoc, right, rrow)
            lset = {rel.get(left["relation_value_field"]) for rel in lrels}
            rset = {rel.get(right["relation_value_field"]) for rel in rrels}
            only_left, only_right = sorted(lset - rset, key=str), sorted(rset - lset, key=str)
            if only_left or only_right:
                findings.append(Finding("relation_set_mismatch", "needs_review", "Связи с компетенциями различаются; нужна ручная сверка.", {
                    "only_in_left": only_left, "only_in_right": only_right, "semesters": sorted(semesters),
                    "source_refs": [_source_ref(ldoc, lrow), _source_ref(rdoc, rrow),
                                    *[_source_ref(ldoc, rel) for rel in lrels], *[_source_ref(rdoc, rel) for rel in rrels]],
                }))
    return _result(rule, checks, findings)


def _normalise(value: Any, operations: Sequence[str]) -> Any:
    if not isinstance(value, str):
        return value
    for operation in operations:
        if operation == "trim":
            value = value.strip()
        elif operation == "collapse_whitespace":
            value = " ".join(value.split())
        elif operation == "casefold":
            value = value.casefold()
        else:
            raise ValidationError(f"Неизвестный способ нормализации: {operation}")
    return value


def _compare_values(rule: Mapping[str, Any], docs: Sequence[Document]) -> RuleResult:
    inputs = rule["inputs"]
    left, right, options = inputs["left"], inputs["right"], inputs["comparison"]
    checks, findings = 0, []
    for ldoc, rdoc in _document_pairs(docs, left["document_type"], right["document_type"]):
        lrows = {row.get(left["key_field"]): row for row in _records(ldoc, left["records_path"])}
        rrows = {row.get(right["key_field"]): row for row in _records(rdoc, right["records_path"])}
        for key in sorted(lrows.keys() | rrows.keys(), key=str):
            checks += 1
            lrow, rrow = lrows.get(key), rrows.get(key)
            if lrow is None or rrow is None:
                doc, row = (ldoc, lrow) if lrow is not None else (rdoc, rrow)
                findings.append(Finding("key_missing_from_document", "needs_review", f"Код компетенции {key!r} есть только в одном документе.", {"source_refs": [_source_ref(doc, row)]}))
                continue
            lv, rv = lrow.get(left["value_field"]), rrow.get(right["value_field"])
            if lv is None or rv is None:
                findings.append(Finding("formulation_missing", "needs_review", f"Не хватает формулировки компетенции {key!r}.", {"source_refs": [_source_ref(ldoc, lrow), _source_ref(rdoc, rrow)]}))
            elif _normalise(lv, options.get("normalize", [])) != _normalise(rv, options.get("normalize", [])):
                findings.append(Finding("formulation_mismatch", "needs_review", f"Формулировки компетенции {key!r} различаются; нужна ручная сверка.", {"source_refs": [_source_ref(ldoc, lrow), _source_ref(rdoc, rrow)], "left_value": lv, "right_value": rv}))
    return _result(rule, checks, findings)


def _references(rule: Mapping[str, Any], docs: Sequence[Document]) -> RuleResult:
    scope, inputs = rule.get("scope", {}), rule["inputs"]
    allowed = set(scope.get("document_types", []))
    target_scope = inputs.get("target_scope", "document")
    if target_scope not in {"document", "program"}:
        raise ValidationError(
            f"Неизвестная область поиска целевой записи: {target_scope}"
        )

    program_targets: dict[str, set[Any]] = defaultdict(set)
    if target_scope == "program":
        for target_doc in docs:
            if not target_doc.program_id:
                continue
            program_targets[target_doc.program_id].update(
                row.get(inputs["target_key_field"])
                for row in _records(target_doc, inputs["target_records_path"])
                if row.get(inputs["target_key_field"]) is not None
            )

    checks, findings = 0, []
    for doc in docs:
        if doc.role not in allowed:
            continue
        if target_scope == "program" and doc.program_id:
            targets = program_targets[doc.program_id]
        else:
            targets = {
                row.get(inputs["target_key_field"])
                for row in _records(doc, inputs["target_records_path"])
                if row.get(inputs["target_key_field"]) is not None
            }
        for row in _records(doc, inputs["source_records_path"]):
            checks += 1
            value = row.get(inputs["source_value_field"])
            if value is None or value not in targets:
                findings.append(Finding("dangling_reference", "failed", f"Ссылка {inputs['source_value_field']}={value!r} не найдена в целевом списке.", {
                    "source_refs": [_source_ref(doc, row)], "known_target_keys": sorted(targets, key=str),
                }))
    return _result(rule, checks, findings)


def _curriculum_relation_context(doc: Document, row: Mapping[str, Any]):
    """Resolve a curriculum relation against its source table row, not its column."""
    if doc.role != "curriculum" or row.get("src_entity") != "curriculum_item":
        return None
    ref = row.get("source_ref") or {}
    if not isinstance(ref, Mapping):
        return None
    fields = ("file", "table", "row")
    locator = tuple(ref.get(k) for k in fields)
    if any(x is None for x in locator):
        return None
    hits = []
    for item in _records(doc, "$.curriculum_items[*]"):
        ir = item.get("source_ref") or {}
        if (isinstance(ir, Mapping) and tuple(ir.get(k) for k in fields) == locator
                and item.get("discipline_name") == row.get("src_key")):
            hits.append(item)
    if len(hits) != 1:
        return None
    item = hits[0]
    semester = item.get("semester")
    section = item["source_ref"].get("section")
    if type(semester) is not int or semester <= 0 or not isinstance(section, str) or not section.strip():
        return None
    if row.get("semester") is not None and row.get("semester") != semester:
        return None
    return semester, section.strip().casefold()


def _ckmv_level(row: Mapping[str, Any]):
    ref = row.get("source_ref") or {}
    section = ref.get("section") if isinstance(ref, Mapping) else None
    if not isinstance(section, str):
        return None
    section = " ".join(section.casefold().split())
    return {"профессиональные перекрестные компетенции базового уровня": "base",
            "профессиональные перекрестные компетенции топ-уровня": "top"}.get(section)


def _unique(rule: Mapping[str, Any], docs: Sequence[Document]) -> RuleResult:
    checks, findings = 0, []
    for doc in docs:
        for collection in rule["inputs"]["collections"]:
            path, fields = collection["records_path"], collection["key_fields"]
            groups: dict[tuple[Any, ...], list[Mapping[str, Any]]] = defaultdict(list)
            for row in _records(doc, path):
                checks += 1
                key = tuple(row.get(name) for name in fields)
                if any(value is None or (isinstance(value, str) and not value.strip()) for value in key):
                    findings.append(Finding("unique_key_missing", "needs_review",
                        f"В {path} документа {doc.label} неполный ключ.", {"source_refs": [_source_ref(doc, row)]}))
                    continue
                if path == "$.relations[*]":
                    key += (row.get("semester"),)
                groups[key].append(row)
            for key, rows in groups.items():
                if len(rows) < 2:
                    continue
                status = "failed"
                if path == "$.relations[*]":
                    contexts = [_curriculum_relation_context(doc, row) for row in rows]
                    if all(ctx is not None for ctx in contexts):
                        if len(set(contexts)) == len(contexts):
                            continue
                    status = "needs_review"
                elif path == "$.competencies[*]":
                    texts = [row.get("formulation") for row in rows]
                    if all(isinstance(t, str) and t.strip() for t in texts):
                        normalized = {normalize_formulation_text(t) for t in texts}
                        if len(normalized) > 1:
                            status = "needs_review"
                        elif doc.role == "ckmv":
                            levels = [_ckmv_level(row) for row in rows]
                            if len(rows) == 2 and set(levels) == {"base", "top"}:
                                continue
                            if None in levels:
                                status = "needs_review"
                findings.append(Finding("duplicate_key", status, f"В {path} документа {doc.label} повторяется ключ {key!r}.", {
                    "key_fields": fields, "source_refs": [_source_ref(doc, row) for row in rows],
                }))
    return _result(rule, checks, findings)


def _semantic_rpd_alignment(
    rule: Mapping[str, Any],
    docs: Sequence[Document],
    semantic_evaluator: SemanticEvaluator | None = None,
) -> RuleResult:
    scope, inputs = rule.get("scope", {}), rule["inputs"]
    checks, findings = 0, []
    allowed = {str(value).lower() for value in scope.get("document_types", [])}
    for doc in docs:
        if doc.role not in allowed:
            continue
        doc_checks, doc_findings = evaluate_rpd_alignment(
            document_label=doc.label,
            disciplines=_records(doc, inputs["disciplines_path"]),
            outcomes=_records(doc, inputs["learning_outcomes_path"]),
            course_content=_records(doc, inputs["course_content_path"]),
            course_content_sections=_records(
                doc, inputs.get("document_sections_path", "$.document_sections[*]")
            ),
            indicators=_records(doc, inputs["indicators_path"]),
            assessment_tools=_records(doc, inputs["assessment_tools_path"]),
            relations=_records(doc, inputs["relations_path"]),
            llm_config=rule.get("llm", {}),
            evaluator=semantic_evaluator,
        )
        checks += doc_checks
        findings.extend(Finding(**item) for item in doc_findings)
    return _result(rule, checks, findings)





def _valid_load(value: Any) -> bool:
    return type(value) is int and value >= 0


def _compare_workload(rule: Mapping[str, Any], docs: Sequence[Document],
                      field_name: str, roles: set[str]) -> RuleResult:
    """Compare only unambiguous semester records of supplied disciplines."""
    checks, findings = 0, []
    for doc in docs:
        if doc.role not in roles:
            continue
        disciplines = _records(doc, "$.disciplines[*]")
        plans = [d for d in docs if d.role == "curriculum"
                 and doc.program_id and d.program_id == doc.program_id]
        if not disciplines:
            checks += 1
            findings.append(Finding("discipline_missing", "needs_review",
                f"{doc.label}: не найдены дисциплины.", {"source_refs": [_source_ref(doc)]}))
        for discipline in disciplines:
            name = discipline.get("name")
            semesters = _semesters(discipline, "semesters")
            refs = [_source_ref(doc, discipline)]
            if (not isinstance(name, str) or not name.strip() or not semesters
                    or any(type(s) is not int or s <= 0 for s in semesters)
                    or not plans or (doc.role == "rpd" and len(semesters) != 1)
                    or (doc.role == "rpp" and len(disciplines) != 1)):
                checks += 1
                findings.append(Finding("workload_comparison_incomplete", "needs_review",
                    f"{doc.label}: недостаточно однозначных данных для сверки «{name}».",
                    {"source_refs": refs, "field": field_name}))
                continue
            for semester in sorted(semesters):
                checks += 1
                matches = [(plan, item) for plan in plans
                           for item in _records(plan, "$.curriculum_items[*]")
                           if item.get("discipline_name") == name and item.get("semester") == semester]
                loads = ([w for w in _records(doc, "$.semester_workload[*]")
                          if w.get("semester") == semester] if doc.role == "rpp" else [discipline])
                evidence = {"source_refs": refs + [_source_ref(d, r) for d, r in matches]
                            + [_source_ref(doc, w) for w in loads if w is not discipline],
                            "semester": semester, "field": field_name}
                if not matches:
                    # Only propose candidates; a matching prefix does not prove identity.
                    normalized = " ".join(name.casefold().split())
                    candidates = []
                    for plan in plans:
                        for item in _records(plan, "$.curriculum_items[*]"):
                            other = item.get("discipline_name")
                            if not isinstance(other, str) or item.get("semester") != semester:
                                continue
                            full = " ".join(other.casefold().split())
                            if full == normalized or full.startswith(normalized + " (") or full.startswith(normalized + ","):
                                candidates.append({"name": other, "semester": semester,
                                    "hours_total": item.get("hours_total"), "credit_units": item.get("credit_units"),
                                    "source_ref": _source_ref(plan, item)})
                    if candidates:
                        findings.append(Finding("workload_match_candidate", "needs_review",
                            f"«{name}», семестр {semester}: найдены кандидаты для ручного сопоставления.",
                            {**evidence, "candidates": candidates, "auto_merge": False,
                             "source_refs": evidence["source_refs"] + [c["source_ref"] for c in candidates]}))
                        continue
                if len(matches) != 1 or len(loads) != 1:
                    findings.append(Finding("workload_pair_missing_or_ambiguous", "needs_review",
                        f"«{name}», семестр {semester}: нет однозначной пары записей нагрузки.", evidence))
                    continue
                plan_doc, plan_row = matches[0]
                load_row = loads[0]
                evidence["source_refs"] = [
                    _source_ref(plan_doc, plan_row),
                    _source_ref(doc, load_row),
                ]
                lhs, rhs = plan_row.get(field_name), load_row.get(field_name)
                if not _valid_load(lhs) or not _valid_load(rhs):
                    findings.append(Finding("hours_data_missing" if field_name == "hours_total" else "value_missing",
                        "needs_review", f"«{name}», семестр {semester}: недостаточно данных нагрузки.", evidence))
                elif lhs != rhs:
                    findings.append(Finding("curriculum_hours_mismatch" if field_name == "hours_total" else "value_mismatch",
                        "failed", f"«{name}», семестр {semester}: УП {lhs}, {doc.role.upper()} {rhs} ({field_name}).",
                        {**evidence, "left_value": lhs, "right_value": rhs}))
    return _result(rule, checks, findings)


def _hours_against_curriculum(rule: Mapping[str, Any], docs: Sequence[Document]) -> RuleResult:
    return _compare_workload(rule, docs, "hours_total", {"rpd", "rpp"})


def _credit_hours(rule: Mapping[str, Any], docs: Sequence[Document]) -> RuleResult:
    """R-04: проверить трудоёмкость там, где одни и те же часы и ЗЕТ сопоставимы."""
    hours_per_credit_unit = rule["inputs"]["hours_per_credit_unit"]
    checks, findings = 0, []
    for doc in docs:
        if doc.role == "curriculum":
            rows = _records(doc, "$.curriculum_items[*]")
        elif doc.role in {"rpd", "rpp"}:
            rows = _records(doc, "$.disciplines[*]") + _records(doc, "$.semester_workload[*]")
        else:
            continue
        for row in rows:
            hours, units = row.get("hours_total"), row.get("credit_units")
            if not isinstance(hours, int) or not isinstance(units, int) or isinstance(hours,bool) or isinstance(units,bool):
                continue
            if units == 0:
                # 0 ЗЕТ = часы вне зачётных единиц (физическая культура):
                # формула часы/ЗЕТ к таким строкам неприменима и нарушением не является.
                continue
            checks += 1
            if hours != units * hours_per_credit_unit:
                findings.append(Finding("credit_hours_mismatch", "failed", f"{doc.label}: {hours} ч не соответствуют {units} ЗЕТ ({hours_per_credit_unit:g} ч/ЗЕТ).", {"credit_units":units,"hours_total":hours,"hours_per_credit_unit":hours_per_credit_unit,"source_refs": [_source_ref(doc,row)]}))
    return _result(rule, checks, findings)


def _title_credit_units(rule: Mapping[str, Any], docs: Sequence[Document]) -> RuleResult:
    checks, findings = 0, []
    for doc in docs:
        if doc.role not in {"rpd", "rpp"}:
            continue
        title = doc.data.get("title_credit_units")
        for discipline in _records(doc, "$.disciplines[*]"):
            table = discipline.get("credit_units")
            if not _valid_load(title) or not _valid_load(table):
                checks += 1
                findings.append(Finding("title_credit_units_missing", "needs_review",
                    f"{doc.label}: недостаточно ЗЕТ для сверки титула и нагрузки.",
                    {"source_refs": [_source_ref(doc, discipline)]}))
                continue
            table_ref = _source_ref(doc, discipline)
            if table_ref.get("table") is None and table_ref.get("section") == "титул":
                continue  # Для РПП без локальной нагрузки ЗЕТ уже взяты с титула.
            checks += 1
            if title != table:
                title_ref = _source_ref(doc, {"source_ref": doc.data.get("title_credit_units_source_ref")})
                findings.append(Finding(
                    "title_credit_units_mismatch",
                    "failed",
                    f"{doc.label}: на титуле {title} ЗЕТ, в разделе нагрузки {table} ЗЕТ.",
                    {"left_value": title, "right_value": table, "source_refs": [title_ref, table_ref]},
                ))
    return _result(rule, checks, findings)


def _semester_workload_totals(rule: Mapping[str, Any], docs: Sequence[Document]) -> RuleResult:
    checks, findings = 0, []
    for doc in _role_docs(docs, "rpp"):
        workloads = _records(doc, "$.semester_workload[*]")
        disciplines = _records(doc, "$.disciplines[*]")
        if len(disciplines) != 1 or not workloads:
            checks += 1
            findings.append(Finding("ambiguous_practice_workload", "needs_review",
                f"{doc.label}: нужна одна практика и её посеместровая нагрузка.",
                {"source_refs": [_source_ref(doc), *[_source_ref(doc, d) for d in disciplines]]}))
            continue
        discipline = disciplines[0]
        semesters = [w.get("semester") for w in workloads]
        expected = _semesters(discipline, "semesters")
        refs = [_source_ref(doc, discipline), *[_source_ref(doc, w) for w in workloads]]
        if (not expected or any(type(s) is not int or s <= 0 for s in semesters)
                or len(set(semesters)) != len(semesters) or set(semesters) != expected):
            checks += 1
            findings.append(Finding("incomplete_semester_workload", "needs_review",
                f"{doc.label}: неполная или неоднозначная нагрузка по семестрам.", {"source_refs": refs}))
            continue
        for key in ("hours_total", "credit_units"):
            checks += 1
            values = [w.get(key) for w in workloads]
            total = discipline.get(key)
            if not _valid_load(total) or any(not _valid_load(v) for v in values):
                findings.append(Finding("semester_value_missing", "needs_review",
                    f"{doc.label}: недостаточно данных {key} для сверки итогов.", {"field": key, "source_refs": refs}))
            elif sum(values) != total:
                findings.append(Finding("semester_total_mismatch", "failed",
                    f"{doc.label}: сумма {key} по семестрам {sum(values)}, итог {total}.",
                    {"field": key, "calculated": sum(values), "declared": total, "source_refs": refs}))
    return _result(rule, checks, findings)


def _outcome_indicator_links(rule: Mapping[str, Any], docs: Sequence[Document]) -> RuleResult:
    """R-10: проверять только явные связи, не домысливая их по тексту."""
    checks, findings = 0, []
    for doc in docs:
        if doc.role not in {"rpd","rpp","ckmv"}:
            continue
        outcomes = _records(doc, "$.learning_outcomes[*]")
        indicators = {r.get("code") for r in _records(doc,"$.indicators[*]")}
        relations = _records(doc,"$.relations[*]")
        explicit = _records(doc,"$.indicator_outcome_links[*]")
        if not outcomes:
            continue
        for link in explicit:
            checks += 1
            code = link.get("indicator_code")
            if code not in indicators:
                findings.append(Finding("unknown_outcome_indicator", "failed", f"Индикатор {code!r} для результата обучения отсутствует в {doc.label}.", {"source_refs": [_source_ref(doc,link)]}))
        # Само отсутствие прямого отношения не является нарушением:
        # РПД/РПП содержат опосредованные связи через компетенцию.
        if not explicit:
            mapped = {(r.get("src_key"),r.get("dst_key")) for r in relations if r.get("src_entity")=="competency" and r.get("dst_entity")=="indicator"}
            for outcome in outcomes:
                via = [r.get("src_key") for r in relations if r.get("src_entity")=="competency" and r.get("dst_entity")=="learning_outcome" and r.get("dst_key")==outcome.get("local_id")]
                checks += 1
                if not via:
                    findings.append(Finding("outcome_without_competency", "needs_review", f"Результат {outcome.get('local_id')} не связан с компетенцией.", {"source_refs": [_source_ref(doc,outcome)]}))
                elif not any((c,ind) in mapped for c in via for ind in indicators):
                    findings.append(Finding("outcome_without_indicator", "needs_review", f"Для результата {outcome.get('local_id')} не найден индикатор через компетенцию.", {"source_refs": [_source_ref(doc,outcome)]}))
    return _result(rule,checks,findings)


def _indicator_hierarchy(rule: Mapping[str, Any], docs: Sequence[Document]) -> RuleResult:
    """R-15: код индикатора начинается с кода своей компетенции."""
    checks, findings = 0, []
    for doc in _role_docs(docs,"ckmv"):
        for indicator in _records(doc,"$.indicators[*]"):
            code, parent = indicator.get("code"), indicator.get("competency_code")
            if not isinstance(code,str) or not isinstance(parent,str):
                continue
            checks += 1
            if not code.startswith(parent + "."):
                findings.append(Finding("indicator_parent_mismatch","failed",f"Индикатор {code!r} не относится к компетенции {parent!r} по коду.",{"source_refs": [_source_ref(doc,indicator)]}))
    return _result(rule,checks,findings)


def _competency_coverage(
    rule: Mapping[str, Any],
    docs: Sequence[Document],
    semantic_evaluator: SemanticEvaluator | None = None,
) -> RuleResult:
    """R-09: полнота покрытия проверяется только при подтверждённом полном УП."""
    curriculum = _role_docs(docs,"curriculum")
    if not rule.get("inputs", {}).get("curriculum_is_complete", False):
        return _result(rule, 0, [])
    checks, findings = 0, []
    for program_doc in [*_role_docs(docs, "characteristic"), *_role_docs(docs, "ckmv")]:
        required = _records(program_doc, "$.competencies[*]")
        for competency in required:
            code = competency.get("code")
            related = [
                document
                for document in curriculum
                if not (
                    document.program_id
                    and program_doc.program_id
                    and document.program_id != program_doc.program_id
                )
            ]
            if not related:
                continue
            checks += 1
            has_curriculum_link = any(
                relation.get("src_entity") == "curriculum_item"
                and relation.get("dst_entity") == "competency"
                and relation.get("kind") == "формирует"
                and relation.get("dst_key") == code
                for document in related
                for relation in _records(document, "$.relations[*]")
            )
            if not has_curriculum_link:
                findings.append(Finding(
                    "competency_not_covered",
                    "failed",
                    f"Компетенция {code!r} не связана с дисциплиной/практикой полного учебного плана.",
                    {"source_refs": [_source_ref(program_doc, competency)]},
                ))
    coverage_docs = [
        CoverageDocument(
            data=doc.data,
            label=doc.label,
            role=doc.role,
            program_id=doc.program_id,
            document_id=doc.document_id,
        )
        for doc in docs
    ]
    semantic_checks, semantic_findings = evaluate_competency_coverage(
        coverage_docs,
        llm_config=rule.get("llm", {}),
        evaluator=semantic_evaluator,
    )
    checks += semantic_checks
    findings.extend(Finding(**item) for item in semantic_findings)
    return _result(rule,checks,findings)



def _required_sections(rule: Mapping[str, Any], docs: Sequence[Document]) -> RuleResult:
    """R-13: только если парсер сохраняет перечень разделов ЦКМВ."""
    required = rule.get("inputs",{}).get("required_sections",[])
    checks, findings = 0, []
    for doc in _role_docs(docs,"ckmv"):
        sections = _records(doc,"$.document_sections[*]")
        if not sections or not required:
            continue
        for title in required:
            checks += 1
            hits = [s for s in sections if str(s.get("title","")).strip().casefold() == str(title).strip().casefold()]
            if not hits or not any(str(s.get("text","")).strip() for s in hits):
                findings.append(Finding("required_section_missing","failed",f"Обязательный раздел ЦКМВ отсутствует или пуст: {title}.",{"source_refs": [_source_ref(doc)]}))
    return _result(rule,checks,findings)


def _education_language(rule: Mapping[str, Any], docs: Sequence[Document]) -> RuleResult:
    """R-14: не угадываем язык по тексту, проверяем только явные поля."""
    checks, findings = 0, []
    for program in _role_docs(docs,"characteristic"):
        info=program.data.get("program",{})
        if not isinstance(info,Mapping):
            continue
        allowed=info.get("education_languages")
        if not isinstance(allowed,list) or not allowed:
            continue
        known={str(lang).strip().casefold() for lang in allowed}
        for doc in docs:
            if doc.role not in {"rpd","rpp"} or (doc.program_id and program.program_id and doc.program_id != program.program_id):
                continue
            language=doc.data.get("education_languages")
            if language is None:
                continue
            for lang in language if isinstance(language,list) else [language]:
                checks += 1
                if str(lang).strip().casefold() not in known:
                    language_ref = _source_ref(doc, {"source_ref": doc.data.get("education_languages_source_ref")})
                    findings.append(Finding("language_not_allowed","failed",f"Язык {lang!r} в {doc.label} не разрешён характеристикой программы.",{"source_refs": [_source_ref(program, info), language_ref]}))
    return _result(rule,checks,findings)


def _code(value: Any) -> str | None:
    return value.strip() if isinstance(value, str) and value.strip() else None


def _program_groups(docs: Sequence[Document]) -> dict[str, list[Document]]:
    """Isolate programs; never compare records with different known program IDs."""
    groups: dict[str, list[Document]] = defaultdict(list)
    for doc in docs:
        groups[doc.program_id or f"__unknown__:{id(doc)}"].append(doc)
    return groups


def _competency_match_candidates(group: Sequence[Document], *, threshold: float = 0.94) -> tuple[int, list[Finding]]:
    """Suggest different-code candidates with ALL source evidence, never merge.

    Similarity is a configurable preliminary heuristic, not proof of equivalence.
    Suppress a pair if the two codes coexist in a single source document.
    """
    if not 0.0 <= threshold <= 1.0:
        raise ValidationError('R-01 match_threshold must be between 0 and 1')
    rows: dict[str, list[tuple[Document, Mapping[str, Any], str]]] = defaultdict(list)
    for doc in group:
        for row in _records(doc, '$.competencies[*]'):
            code, formulation = _code(row.get('code')), row.get('formulation')
            if code and isinstance(formulation, str) and formulation.strip():
                rows[code].append((doc, row, normalize_formulation_text(formulation)))
    pair_findings, checks = [], 0
    codes = sorted(rows)
    for i, first in enumerate(codes):
        for second in codes[i + 1:]:
            if {id(doc) for doc, _, _ in rows[first]} & {id(doc) for doc, _, _ in rows[second]}:
                continue
            evidence_pairs = []
            matched_variants = []
            best = 0.0
            for left_doc, left_row, left_text in rows[first]:
                for right_doc, right_row, right_text in rows[second]:
                    if left_doc is right_doc or not left_text or not right_text:
                        continue
                    checks += 1
                    similarity = SequenceMatcher(None, left_text, right_text).ratio()
                    if similarity < threshold:
                        continue
                    best = max(best, similarity)
                    left_variant = {'code': first, 'formulation': left_row['formulation'],
                                    'document': left_doc.label, 'source_ref': _source_ref(left_doc, left_row)}
                    right_variant = {'code': second, 'formulation': right_row['formulation'],
                                     'document': right_doc.label, 'source_ref': _source_ref(right_doc, right_row)}
                    evidence_pairs.append({'left': left_variant, 'right': right_variant,
                                           'similarity': round(similarity, 4)})
                    for variant in (left_variant, right_variant):
                        if variant not in matched_variants:
                            matched_variants.append(variant)
            if not evidence_pairs:
                continue
            matched_variants.sort(key=lambda v: (v['code'], v['document'], str(v['source_ref'])))
            pair_findings.append({
                'codes': (first, second),
                'best': best,
                'evidence_pairs': evidence_pairs,
                'variants': matched_variants,
            })

    # A code can be similar to several other codes. Pairwise findings would
    # then offer overlapping document edits (A↔B and B↔C both rewrite B's
    # source fields), making it impossible to save a consistent set of
    # decisions. Present each connected candidate group once so the user
    # chooses one code for the whole group or keeps the group separate.
    parents = {code: code for edge in pair_findings for code in edge['codes']}

    def root(code):
        while parents[code] != code:
            parents[code] = parents[parents[code]]
            code = parents[code]
        return code

    for edge in pair_findings:
        left, right = map(root, edge['codes'])
        if left != right:
            parents[max(left, right)] = min(left, right)

    groups: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for edge in pair_findings:
        groups[root(edge['codes'][0])].append(edge)

    findings = []
    for edges in sorted(
        groups.values(),
        key=lambda items: tuple(sorted({code for edge in items for code in edge['codes']})),
    ):
        candidate_codes = sorted({code for edge in edges for code in edge['codes']})
        matched_variants = []
        evidence_pairs = []
        for edge in edges:
            evidence_pairs.extend(edge['evidence_pairs'])
            for variant in edge['variants']:
                if variant not in matched_variants:
                    matched_variants.append(variant)
        matched_variants.sort(
            key=lambda v: (v['code'], v['document'], str(v['source_ref']))
        )
        findings.append(Finding(
            'competency_code_match_candidate', 'needs_review',
            f'Возможное соответствие кодов {", ".join(candidate_codes)}; '
            'выберите один код для всей группы или оставьте коды разными.',
            {'codes': candidate_codes, 'program_id': group[0].program_id,
             'similarity': round(max(edge['best'] for edge in edges), 4),
             'match_threshold': threshold,
             'match_method': 'normalized_formulation_similarity',
             'heuristic_only': True, 'evidence_pairs': evidence_pairs,
             'variants': matched_variants,
             'source_refs': [v['source_ref'] for v in matched_variants],
             'requires_user_confirmation': True, 'auto_merge': False}))
    return checks, findings


def _competency_code_match_candidates(
    rule: Mapping[str, Any], docs: Sequence[Document],
) -> RuleResult:
    """R-01 before graph building: suggest code matches for user decisions only."""
    checks, findings = 0, []
    threshold = float(rule.get("match_threshold", 0.94))
    for group in _program_groups(docs).values():
        group_checks, candidates = _competency_match_candidates(group, threshold=threshold)
        checks += group_checks
        findings.extend(candidates)
    return _result(rule, checks, findings)


def _competency_consistency(
    rule: Mapping[str, Any], docs: Sequence[Document],
) -> RuleResult:
    """R-01-graph after user decisions: validate codes and final graph links.

    No completeness is inferred from a partial program catalogue or curriculum.
    """
    checks, findings = 0, []
    for pid, group in _program_groups(docs).items():
        declared: dict[str, list[tuple[Document, Mapping[str, Any]]]] = defaultdict(list)
        authoritative: set[str] = set()
        for doc in group:
            for row in _records(doc, '$.competencies[*]'):
                code = _code(row.get('code'))
                if code:
                    declared[code].append((doc, row))
                    if doc.role in {'ckmv', 'characteristic'}:
                        authoritative.add(code)
        code_findings: dict[str, Finding] = {}
        code_sources: dict[str, dict[str, dict[str, Any]]] = defaultdict(dict)
        for doc in group:
            for rel in _records(doc, '$.relations[*]'):
                if rel.get('dst_entity') != 'competency' and rel.get('src_entity') != 'competency':
                    continue
                code = _code(rel.get('dst_key') if rel.get('dst_entity') == 'competency' else rel.get('src_key'))
                if not code:
                    continue
                checks += 1
                if code not in declared:
                    code_findings.setdefault(code, Finding('unknown_competency_code', 'failed',
                        f'Связи используют неизвестный код компетенции {code}.',
                        {'code': code, 'program_id': pid, 'occurrence_count': 0}))
                elif authoritative and code not in authoritative:
                    # A partial CKMV/characteristic list is not an exhaustive catalogue.
                    code_findings.setdefault(code, Finding('competency_not_in_program_catalogue', 'needs_review',
                        f'Код {code} не найден в доступных справочниках программы.',
                        {'code': code, 'program_id': pid, 'occurrence_count': 0}))
                else:
                    continue
                code_findings[code].evidence['occurrence_count'] += 1
                ref = _source_ref(doc, rel)
                code_sources[code][json.dumps(ref, sort_keys=True, ensure_ascii=False)] = ref
        for code, finding in sorted(code_findings.items()):
            for document, row in declared.get(code, []):
                ref = _source_ref(document, row)
                code_sources[code][json.dumps(ref, sort_keys=True, ensure_ascii=False)] = ref
            finding.evidence['source_refs'] = list(code_sources[code].values())
            findings.append(finding)
        # Compare only disciplines explicitly represented in the supplied curriculum.
        # A curriculum_item can occur in several semesters; match both name and semester.
        for plan in (d for d in group if d.role == 'curriculum'):
            plan_rels = _records(plan, '$.relations[*]')
            for item in _records(plan, '$.curriculum_items[*]'):
                name, semester = item.get('discipline_name'), item.get('semester')
                if not isinstance(name, str) or semester is None:
                    continue
                related = [r for r in plan_rels if r.get('src_entity') == 'curriculum_item'
                    and r.get('dst_entity') == 'competency' and r.get('kind') == 'формирует'
                    and r.get('src_key') in (name, f'{name}::semester={semester}')]
                plan_codes = {_code(r.get('dst_key')) for r in related} - {None}
                for doc in (d for d in group if d.role in {'rpd', 'rpp'}):
                    for discipline in _records(doc, '$.disciplines[*]'):
                        if discipline.get('name') != name or semester not in _semesters(discipline, 'semesters'):
                            continue
                        doc_rels = [r for r in _records(doc, '$.relations[*]')
                            if r.get('src_entity') == 'discipline' and r.get('src_key') == name
                            and r.get('dst_entity') == 'competency' and r.get('kind') == 'формирует']
                        doc_codes = {_code(r.get('dst_key')) for r in doc_rels} - {None}
                        checks += 1
                        refs = [_source_ref(plan, item), _source_ref(doc, discipline),
                            *[_source_ref(plan, r) for r in related], *[_source_ref(doc, r) for r in doc_rels]]
                        if not related or not doc_rels:
                            findings.append(Finding('competency_links_incomplete', 'needs_review',
                                f'Недостаточно связей для проверки компетенций «{name}» в семестре {semester}.',
                                {'semester': semester, 'source_refs': refs}))
                        elif plan_codes != doc_codes:
                            findings.append(Finding('competency_relation_mismatch', 'failed',
                                f'Компетенции «{name}» в семестре {semester} различаются в УП и {doc.role.upper()}.',
                                {'semester': semester, 'only_in_curriculum': sorted(plan_codes - doc_codes),
                                 'only_in_document': sorted(doc_codes - plan_codes), 'source_refs': refs}))
    return _result(rule, checks, findings)


def _competency_formulations(rule: Mapping[str, Any], docs: Sequence[Document]) -> RuleResult:
    """R-07: collect different formulations of each competency code for user review."""
    checks, findings = 0, []
    for program_id, group in _program_groups(docs).items():
        by_code: dict[str, list[tuple[Document, Mapping[str, Any]]]] = defaultdict(list)
        for doc in group:
            if doc.role not in {'rpd', 'rpp', 'ckmv', 'characteristic', 'curriculum', 'matrix'}:
                continue
            for row in _records(doc, '$.competencies[*]'):
                code = _code(row.get('code'))
                if code:
                    by_code[code].append((doc, row))

        for code, records in sorted(by_code.items()):
            variants = [
                {'document': doc.label, 'code': code, 'formulation': row.get('formulation'),
                 'source_ref': _source_ref(doc, row)}
                for doc, row in records
            ]
            differs = False
            missing = False
            for index, (left_doc, left_row) in enumerate(records):
                for right_doc, right_row in records[index + 1:]:
                    if left_doc is right_doc:
                        continue
                    checks += 1
                    left, right = left_row.get('formulation'), right_row.get('formulation')
                    if not isinstance(left, str) or not left.strip() or not isinstance(right, str) or not right.strip():
                        missing = True
                    elif (
                        normalize_variant_formulation_text(left)
                        != normalize_variant_formulation_text(right)
                    ):
                        differs = True

            if not missing and not differs:
                continue
            findings.append(Finding(
                'competency_formulation_missing' if missing else 'competency_formulation_review',
                'needs_review',
                f'Варианты формулировки компетенции {code} в разных документах требуют выбора пользователя.',
                {'code': code, 'program_id': program_id,
                 'variants': variants,
                 'source_refs': [variant['source_ref'] for variant in variants],
                 'requires_user_confirmation': True,
                 'auto_select': False},
            ))
    return _result(rule, checks, findings)


def _indicator_formulation_consistency(rule: Mapping[str, Any], docs: Sequence[Document]) -> RuleResult:
    scope, inputs = rule.get("scope", {}), rule["inputs"]
    allowed = {str(value).lower() for value in scope.get("document_types", [])}
    indicator_documents = []
    for doc in docs:
        if doc.role not in allowed:
            continue
        indicators = []
        for row in _records(doc, inputs["indicators_path"]):
            item = dict(row)
            item["_source_ref"] = _source_ref(doc, row)
            indicators.append(item)
        indicator_documents.append(IndicatorDocument(
            label=doc.label,
            document_type=doc.role or "unknown",
            program_id=doc.program_id,
            indicators=indicators,
        ))

    checks, raw_findings = evaluate_indicator_formulations(documents=indicator_documents)
    return _result(rule, checks, [Finding(**item) for item in raw_findings])


HANDLERS = {
    "competency_consistency": _competency_consistency,
    "competency_code_match_candidates": _competency_code_match_candidates,
    "competency_formulations": _competency_formulations,
    "sum_components_equals_total": _sum_hours,
    "hours_against_curriculum": _hours_against_curriculum,
    "credit_hours": _credit_hours,
    "title_credit_units": _title_credit_units,
    "semester_workload_totals": _semester_workload_totals,
    "outcome_indicator_links": _outcome_indicator_links,
    "indicator_hierarchy": _indicator_hierarchy,
    "competency_coverage": _competency_coverage,
    "required_sections": _required_sections,
    "education_language": _education_language,
    "compare_fields_across_documents": _compare_fields,
    "compare_relation_sets": _compare_relations,
    "compare_values_by_key": _compare_values,
    "reference_exists": _references,
    "unique_by_fields": _unique,
    "semantic_rpd_alignment": _semantic_rpd_alignment,
    "indicator_formulation_consistency": _indicator_formulation_consistency,
}
SEMANTIC_RULE_TYPES = {
    "semantic_rpd_alignment",
    "competency_coverage",
}


def run_validation(
    documents: Sequence[Mapping[str, Any] | Document],
    rules_config: Mapping[str, Any] | None = None,
    *,
    phase: ValidationPhase = "document_reconciliation",
    semantic_evaluator: SemanticEvaluator | None = None,
    rule_codes: Sequence[str] | None = None,
) -> ValidationReport:
    """Проверить документы либо проекцию актуальной версии графа выбранной фазой.

    Для graph_validation вызывающая сторона должна передать JSON-проекции той
    версии графа, которую проверяет, а не исходный, уже устаревший пакет.
    """
    if phase not in VALIDATION_PHASES:
        raise ValidationError(f"Неизвестная фаза валидации: {phase}")
    config = _validate_rules_config(rules_config) if rules_config is not None else load_rules()
    docs = _documents(documents)
    selected_rule_codes = tuple(rule_codes) if rule_codes is not None else None
    requested_rule_codes = None
    if selected_rule_codes is not None:
        requested_rule_codes = set(selected_rule_codes)
        if len(requested_rule_codes) != len(selected_rule_codes):
            raise ValidationError("Список ручных правил содержит повторы")
        available_manual_rules = {
            rule["id"]
            for rule in config.get("rules", [])
            if rule.get("status") in RUNNABLE_RULE_STATUSES
            and rule.get("run_mode", "automatic") == "manual"
            and phase in rule.get("phases", [])
        }
        invalid_codes = requested_rule_codes - available_manual_rules
        if invalid_codes:
            raise ValidationError(
                "Нельзя запустить вручную правила: " + ", ".join(sorted(invalid_codes))
            )
    results = []
    for rule in config.get("rules", []):
        if rule["status"] not in RUNNABLE_RULE_STATUSES:
            continue
        if phase not in rule.get("phases", []):
            continue
        if requested_rule_codes is None and rule.get("run_mode", "automatic") == "manual":
            continue
        if requested_rule_codes is not None and rule["id"] not in requested_rule_codes:
            continue
        handler = HANDLERS.get(rule.get("type"))
        if handler is None:
            raise ValidationError(f"Нет обработчика типа {rule.get('type')!r}")
        if rule.get("type") in SEMANTIC_RULE_TYPES:
            results.append(handler(rule, docs, semantic_evaluator))
        else:
            results.append(handler(rule, docs))
    return ValidationReport(phase, results)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Проверить JSON по rules.yaml")
    parser.add_argument("json_files", nargs="+", type=Path)
    parser.add_argument("--rules", type=Path, default=DEFAULT_RULES)
    parser.add_argument(
        "--phase",
        choices=sorted(VALIDATION_PHASES),
        default="document_reconciliation",
        help="graph_validation принимает проекции проверяемой версии графа",
    )
    args = parser.parse_args(argv)
    try:
        docs = [json.loads(path.read_text(encoding="utf-8")) for path in args.json_files]
        report = run_validation(docs, load_rules(args.rules), phase=args.phase)
    except (OSError, json.JSONDecodeError, ValidationError) as exc:
        print(json.dumps({"error": str(exc)}, ensure_ascii=False, indent=2), file=sys.stderr)
        return 2
    print(json.dumps(report.to_dict(), ensure_ascii=False, indent=2))
    # Обнаруженные нарушения не задают сами по себе политику блокировки правок.
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
