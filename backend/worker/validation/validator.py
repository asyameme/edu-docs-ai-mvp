from __future__ import annotations

import argparse
import json
import sys
from collections import defaultdict
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Mapping, Sequence
import unicodedata


if __package__:
    from .semantic_rpd import SemanticEvaluator, evaluate_rpd_alignment
    from .semantic_competency import evaluate_competency_pair, CompetencyEvaluator
else:  # Поддерживаем и прямой запуск validator.py.
    from semantic_rpd import SemanticEvaluator, evaluate_rpd_alignment
    from semantic_competency import evaluate_competency_pair, CompetencyEvaluator

try:
    import yaml
except ImportError:  # PyYAML устанавливается из backend/requirements.txt
    yaml = None


DEFAULT_RULES = Path(__file__).with_name("rules.yaml")
DOC_TYPES = {"rpd", "rpp", "curriculum", "matrix", "characteristic", "ckmv"}


class ValidationError(Exception):
    """Ошибка чтения конфигурации или входных JSON."""


@dataclass(frozen=True)
class Document:
    data: Mapping[str, Any]
    label: str

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
    results: list[RuleResult]

    def to_dict(self) -> dict[str, Any]:
        counts: dict[str, int] = defaultdict(int)
        for result in self.results:
            counts[result.status] += 1
        return {
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
    if not isinstance(config, dict) or config.get("schema_version") != 1:
        raise ValidationError("Ожидается rules.yaml со schema_version: 1")
    if not isinstance(config.get("rules"), list):
        raise ValidationError("Поле rules в rules.yaml должно быть списком")
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
        return {"file": document.label, **ref}
    return {"file": document.label}


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
            if not isinstance(total, int) or isinstance(total, bool) or not isinstance(components, Mapping):
                findings.append(Finding("hours_data_missing", "needs_review", f"Нельзя проверить сумму часов для «{name}».", {"source_refs": [_source_ref(doc, row)]}))
                continue
            included = {key: components[key] for key in inputs["include_component_keys"] if key in components}
            if any(not isinstance(value, int) or isinstance(value, bool) for value in included.values()):
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
    left, right = inputs["left"], inputs["right"]
    checks, findings = 0, []
    for ldoc, rdoc in _document_pairs(docs, left["document_type"], right["document_type"]):
        for lrow, rrow, semesters in _matched_records(ldoc, rdoc, left, right, inputs["matching"]):
            checks += 1
            lv, rv = lrow.get(left["value_field"]), rrow.get(right["value_field"])
            refs = [_source_ref(ldoc, lrow), _source_ref(rdoc, rrow)]
            name = lrow.get(left["name_field"])
            if lv is None or rv is None:
                findings.append(Finding("value_missing", "needs_review", f"Не хватает сопоставимого значения для «{name}».", {"source_refs": refs, "semesters": sorted(semesters)}))
            elif lv != rv:
                findings.append(Finding("value_mismatch", "failed", f"Для «{name}» в семестре(ах) {sorted(semesters)} значения {lv!r} и {rv!r} различаются.", {"source_refs": refs, "left_value": lv, "right_value": rv, "semesters": sorted(semesters)}))
    return _result(rule, checks, findings)


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
    checks, findings = 0, []
    for doc in docs:
        if doc.role not in allowed:
            continue
        targets = {row.get(inputs["target_key_field"]) for row in _records(doc, inputs["target_records_path"])}
        for row in _records(doc, inputs["source_records_path"]):
            checks += 1
            value = row.get(inputs["source_value_field"])
            if value is None or value not in targets:
                findings.append(Finding("dangling_reference", "failed", f"Ссылка {inputs['source_value_field']}={value!r} не найдена в целевом списке.", {
                    "source_refs": [_source_ref(doc, row)], "known_target_keys": sorted((item for item in targets if item is not None), key=str),
                }))
    return _result(rule, checks, findings)


def _unique(rule: Mapping[str, Any], docs: Sequence[Document]) -> RuleResult:
    checks, findings = 0, []
    for doc in docs:
        for collection in rule["inputs"]["collections"]:
            path, fields = collection["records_path"], collection["key_fields"]
            groups: dict[tuple[Any, ...], list[Mapping[str, Any]]] = defaultdict(list)
            for row in _records(doc, path):
                checks += 1
                groups[tuple(row.get(name) for name in fields)].append(row)
            for key, rows in groups.items():
                if len(rows) > 1:
                    findings.append(Finding("duplicate_key", "failed", f"В {path} документа {doc.label} повторяется ключ {key!r}.", {
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
            indicators=_records(doc, inputs["indicators_path"]),
            assessment_tools=_records(doc, inputs["assessment_tools_path"]),
            relations=_records(doc, inputs["relations_path"]),
            llm_config=rule.get("llm", {}),
            evaluator=semantic_evaluator,
        )
        checks += doc_checks
        findings.extend(Finding(**item) for item in doc_findings)
    return _result(rule, checks, findings)




def _hours_against_curriculum(rule: Mapping[str, Any], docs: Sequence[Document]) -> RuleResult:
    """R-02: сравнить общую нагрузку одинаковой дисциплины в одном семестре."""
    checks, findings = 0, []
    for curriculum in _role_docs(docs, "curriculum"):
        for item in _records(curriculum, "$.curriculum_items[*]"):
            name, semester = item.get("discipline_name"), item.get("semester")
            for doc in docs:
                if doc.role not in {"rpd", "rpp"} or (doc.program_id and curriculum.program_id and doc.program_id != curriculum.program_id):
                    continue
                for discipline in _records(doc, "$.disciplines[*]"):
                    if discipline.get("name") != name or semester not in _semesters(discipline, "semesters"):
                        continue
                    # Общая нагрузка РПП охватывает все семестры; сравниваем только
                    # семестровую запись, иначе получили бы ложные ошибки.
                    workload = [w for w in _records(doc, "$.semester_workload[*]") if w.get("semester") == semester] if doc.role == "rpp" else []
                    if doc.role == "rpp" and not workload:
                        continue
                    if doc.role == "rpd" and len(_semesters(discipline, "semesters")) != 1:
                        continue
                    actual = workload[0] if workload else discipline
                    checks += 1
                    lhs, rhs = item.get("hours_total"), actual.get("hours_total")
                    if not isinstance(lhs, int) or not isinstance(rhs, int) or isinstance(lhs,bool) or isinstance(rhs,bool):
                        findings.append(Finding("hours_data_missing", "needs_review", f"Недостаточно часов для сверки «{name}», семестр {semester}.", {"source_refs": [_source_ref(curriculum,item),_source_ref(doc,actual)]}))
                    elif lhs != rhs:
                        findings.append(Finding("curriculum_hours_mismatch", "failed", f"«{name}», семестр {semester}: в УП {lhs} ч, в {doc.role.upper()} {rhs} ч.", {"semester":semester,"left_value":lhs,"right_value":rhs,"source_refs": [_source_ref(curriculum,item),_source_ref(doc,actual)]}))
    return _result(rule, checks, findings)


def _credit_hours(rule: Mapping[str, Any], docs: Sequence[Document]) -> RuleResult:
    """R-04: проверить трудоёмкость там, где одни и те же часы и ЗЕТ сопоставимы."""
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
            checks += 1
            if hours != units * 36:
                findings.append(Finding("credit_hours_mismatch", "failed", f"{doc.label}: {hours} ч не соответствуют {units} ЗЕТ (36 ч/ЗЕТ).", {"credit_units":units,"hours_total":hours,"source_refs": [_source_ref(doc,row)]}))
    return _result(rule, checks, findings)


def _semester_workload_totals(rule: Mapping[str, Any], docs: Sequence[Document]) -> RuleResult:
    """R-03/R-05: итоги практики равны суммам по отдельным семестрам."""
    checks, findings = 0, []
    for doc in _role_docs(docs,"rpp"):
        workloads = _records(doc,"$.semester_workload[*]")
        if not workloads:
            continue
        for discipline in _records(doc,"$.disciplines[*]"):
            for key in ("hours_total", "credit_units"):
                values = [w.get(key) for w in workloads]
                total = discipline.get(key)
                if not isinstance(total,int) or isinstance(total,bool) or any(not isinstance(v,int) or isinstance(v,bool) for v in values):
                    continue
                checks += 1
                if sum(values) != total:
                    findings.append(Finding("semester_total_mismatch", "failed", f"{doc.label}: сумма {key} по семестрам {sum(values)}, итог {total}.", {"field":key,"calculated":sum(values),"declared":total,"source_refs": [_source_ref(doc,discipline),*[_source_ref(doc,w) for w in workloads]]}))
    return _result(rule,checks,findings)


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


def _competency_coverage(rule: Mapping[str, Any], docs: Sequence[Document]) -> RuleResult:
    """R-09: полнота покрытия проверяется только при подтверждённом полном УП."""
    curriculum = _role_docs(docs,"curriculum")
    if not rule.get("inputs",{}).get("curriculum_is_complete",False) or not curriculum:
        return _result(rule,0,[])
    checks, findings = 0, []
    for program_doc in [*_role_docs(docs,"characteristic"),*_role_docs(docs,"ckmv")]:
        required = _records(program_doc,"$.competencies[*]")
        for competency in required:
            code = competency.get("code")
            related = [c for c in curriculum if not (c.program_id and program_doc.program_id and c.program_id != program_doc.program_id)]
            if not related:
                continue
            checks += 1
            if not any(any(r.get("dst_entity")=="competency" and r.get("dst_key")==code and r.get("src_entity")=="curriculum_item" for r in _records(c,"$.relations[*]")) for c in related):
                findings.append(Finding("competency_not_covered","failed",f"Компетенция {code!r} не связана с дисциплиной/практикой полного учебного плана.",{"source_refs": [_source_ref(program_doc,competency)]}))
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
                    findings.append(Finding("language_not_allowed","failed",f"Язык {lang!r} в {doc.label} не разрешён характеристикой программы.",{"source_refs": [_source_ref(program),_source_ref(doc)]}))
    return _result(rule,checks,findings)


def _code(value: Any) -> str | None:
    return value.strip() if isinstance(value, str) and value.strip() else None


def _program_groups(docs: Sequence[Document]) -> dict[str, list[Document]]:
    """Isolate programs; never compare records with different known program IDs."""
    groups: dict[str, list[Document]] = defaultdict(list)
    for doc in docs:
        groups[doc.program_id or f"__unknown__:{id(doc)}"].append(doc)
    return groups


def _competency_consistency(rule: Mapping[str, Any], docs: Sequence[Document]) -> RuleResult:
    """R-01 without matrix: validate codes, links, and matched curriculum coverage.

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
        for doc in group:
            for rel in _records(doc, '$.relations[*]'):
                if rel.get('dst_entity') != 'competency' and rel.get('src_entity') != 'competency':
                    continue
                code = _code(rel.get('dst_key') if rel.get('dst_entity') == 'competency' else rel.get('src_key'))
                if not code:
                    continue
                checks += 1
                if code not in declared:
                    findings.append(Finding('unknown_competency_code', 'failed',
                        f'В {doc.label} связь использует неизвестный код компетенции {code}.',
                        {'code': code, 'source_refs': [_source_ref(doc, rel)]}))
                elif authoritative and code not in authoritative:
                    # A partial CKMV/characteristic list is not an exhaustive catalogue.
                    findings.append(Finding('competency_not_in_program_catalogue', 'needs_review',
                        f'Код {code} не найден в доступных справочниках программы.',
                        {'code': code, 'source_refs': [_source_ref(doc, rel),
                          *[_source_ref(d, r) for d, r in declared[code]]]}))
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


def _canonical_formulation(value: str) -> str:
    value = unicodedata.normalize('NFKC', value).casefold()
    return ' '.join(value.split()).rstrip(' .,!?:;')


def _competency_formulations(rule: Mapping[str, Any], docs: Sequence[Document],
                             evaluator: CompetencyEvaluator | None = None) -> RuleResult:
    """R-07: canonical reference comparison with optional real semantic judgment.

    Different codes are never treated as equivalent. Without a semantic model,
    a nontrivial text difference is marked for manual review, never passed.
    """
    checks, findings = 0, []
    for pid, group in _program_groups(docs).items():
        by_code: dict[str, list[tuple[Document, Mapping[str, Any]]]] = defaultdict(list)
        for doc in group:
            if doc.role not in {'rpd', 'rpp', 'ckmv', 'characteristic', 'curriculum'}:
                continue
            for row in _records(doc, '$.competencies[*]'):
                code = _code(row.get('code'))
                if code:
                    by_code[code].append((doc, row))
        for code, records in sorted(by_code.items()):
            # Prefer CKMV as the comparative reference; characteristic is fallback.
            baseline = next(((d, r) for role in ('ckmv', 'characteristic')
                             for d, r in records if d.role == role and _code(r.get('formulation'))), None)
            if baseline is None:
                # Without an approved catalogue, cross-document text comparisons
                # must not be presented as normative validity.
                continue
            bdoc, brow = baseline
            base_text = brow['formulation']
            for doc, row in records:
                if doc is bdoc and row is brow:
                    continue
                checks += 1
                text = row.get('formulation')
                refs = [_source_ref(bdoc, brow), _source_ref(doc, row)]
                if not isinstance(text, str) or not text.strip():
                    findings.append(Finding('competency_formulation_missing', 'needs_review',
                        f'Нет формулировки компетенции {code} в {doc.label}.',
                        {'code': code, 'source_refs': refs}))
                    continue
                if _canonical_formulation(base_text) == _canonical_formulation(text):
                    continue
                outcome = evaluate_competency_pair(code, base_text, text, evaluator=evaluator,
                                                  config=rule.get('llm', {}))
                if outcome['status'] == 'equivalent':
                    continue
                status = 'failed' if outcome['status'] == 'changed' else 'needs_review'
                findings.append(Finding('competency_formulation_mismatch' if status == 'failed' else 'competency_formulation_review',
                    status, f'Формулировка компетенции {code} отличается от источника {bdoc.label}.',
                    {'code': code, 'reference_text': base_text, 'document_text': text,
                     'semantic_status': outcome['status'], 'reason': outcome['reason'],
                     'source_refs': refs}))
    return _result(rule, checks, findings)


HANDLERS = {
    "competency_consistency": _competency_consistency,
    "competency_formulations": _competency_formulations,
    "sum_components_equals_total": _sum_hours,
    "hours_against_curriculum": _hours_against_curriculum,
    "credit_hours": _credit_hours,
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
}


def run_validation(
    documents: Sequence[Mapping[str, Any] | Document],
    rules_config: Mapping[str, Any] | None = None,
    *,
    semantic_evaluator: SemanticEvaluator | None = None,
    competency_evaluator: CompetencyEvaluator | None = None,
) -> ValidationReport:
    config = rules_config if rules_config is not None else load_rules()
    docs = _documents(documents)
    results = []
    for rule in config.get("rules", []):
        handler = HANDLERS.get(rule.get("type"))
        if handler is None:
            raise ValidationError(f"Нет обработчика типа {rule.get('type')!r}")
        if rule.get("type") == "competency_formulations":
            results.append(handler(rule, docs, competency_evaluator))
        elif rule.get("type") == "semantic_rpd_alignment":
            results.append(handler(rule, docs, semantic_evaluator))
        else:
            results.append(handler(rule, docs))
    return ValidationReport(results)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Проверить JSON по rules.yaml")
    parser.add_argument("json_files", nargs="+", type=Path)
    parser.add_argument("--rules", type=Path, default=DEFAULT_RULES)
    args = parser.parse_args(argv)
    try:
        docs = [json.loads(path.read_text(encoding="utf-8")) for path in args.json_files]
        report = run_validation(docs, load_rules(args.rules))
    except (OSError, json.JSONDecodeError, ValidationError) as exc:
        print(json.dumps({"error": str(exc)}, ensure_ascii=False, indent=2), file=sys.stderr)
        return 2
    print(json.dumps(report.to_dict(), ensure_ascii=False, indent=2))
    # Обнаруженные нарушения не задают сами по себе политику блокировки правок.
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
