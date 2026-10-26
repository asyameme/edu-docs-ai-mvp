"""Запуск проверок, описанных в rules.yaml."""
from __future__ import annotations

import argparse
import json
import sys
from collections import defaultdict
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Mapping, Sequence

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


HANDLERS = {
    "sum_components_equals_total": _sum_hours,
    "compare_fields_across_documents": _compare_fields,
    "compare_relation_sets": _compare_relations,
    "compare_values_by_key": _compare_values,
    "reference_exists": _references,
    "unique_by_fields": _unique,
}


def run_validation(
    documents: Sequence[Mapping[str, Any] | Document],
    rules_config: Mapping[str, Any] | None = None,
) -> ValidationReport:
    config = rules_config if rules_config is not None else load_rules()
    docs = _documents(documents)
    results = []
    for rule in config.get("rules", []):
        handler = HANDLERS.get(rule.get("type"))
        if handler is None:
            raise ValidationError(f"Нет обработчика типа {rule.get('type')!r}")
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
