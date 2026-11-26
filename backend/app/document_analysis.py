"""Документная стадия существующего валидатора в формате AnalysisV2 без графа."""

from copy import deepcopy
from uuid import UUID, uuid5

from app.document_reconciliation import (
    validate_document_mutation_variants,
)
from app.errors import DomainError
from app.review_logic import digest
from app.validation_bridge import (
    run_document_rules,
    validation_documents,
    validation_rule_revision,
    validation_ruleset,
)
from worker.validation.text_normalization import (
    format_variant_formulation_text,
    normalize_variant_formulation_text,
)
from worker.validation.validator import load_rules

_FIELDS = {
    "curriculum_hours_mismatch": "hours_total",
    "value_mismatch": "credit_units",
    "competency_formulation_review": "formulation",
    "competency_formulation_mismatch": "formulation",
    "competency_formulation_missing": "formulation",
    "competency_code_match_candidate": "code",
    # R-08 groups different formulations of the same indicator for user choice.
    "indicator_formulation_review": "text",
}
_PROTECTED = {"meta", "source_ref", "parse_issues"}


def _locator(value):
    return {k: v for k, v in value.items() if k not in {"file", "document_id"} and v is not None}


def _records(value, path=""):
    if isinstance(value, dict):
        if isinstance(value.get("source_ref"), dict):
            yield path, value
        for key, child in value.items():
            if key not in _PROTECTED:
                escaped = key.replace("~", "~0").replace("/", "~1")
                yield from _records(child, path + "/" + escaped)
    elif isinstance(value, list):
        for index, child in enumerate(value):
            yield from _records(child, path + "/" + str(index))


def _sources(finding, documents):
    """Сопоставляет доказательства с документами, не выбирая первый одинаковый файл."""
    sources, limitations = [], []
    evidence = finding["evidence"]
    variants = evidence.get("variants", [])
    references = []
    if isinstance(variants, list):
        references.extend(
            (variant.get("source_ref"), variant)
            for variant in variants
            if isinstance(variant, dict) and isinstance(variant.get("source_ref"), dict)
        )
    if not references:
        references.extend((ref, None) for ref in evidence.get("source_refs", []))
    seen = set()
    for ref, variant in references:
        ref_key = digest([ref, variant])
        if ref_key in seen:
            continue
        seen.add(ref_key)
        identity = ref.get("document_id")
        candidates = [
            item for item in documents
            if (str(item["document_id"]) == str(identity) if identity is not None
                else item["parsed"].get("meta", {}).get("file_name") == ref.get("file"))
        ]
        if len(candidates) != 1:
            limitations.append("Документ источника не определён однозначно")
            continue
        item = candidates[0]
        matches = [
            (path, row) for path, row in _records(item["parsed"])
            if _locator(row["source_ref"]) == _locator(ref)
        ]
        if variant is not None:
            code = variant.get("code")
            matches = [
                (path, row) for path, row in matches
                if (code is None or isinstance(row.get("code"), str)
                    and row["code"].strip() == code)
                and ("formulation" not in variant
                     or row.get("formulation") == variant["formulation"])
                and ("text" not in variant or isinstance(row.get("text"), str)
                     and row["text"].strip() == variant["text"])
            ]
        sources.append((str(item["document_id"]), matches))
    return sources, limitations


def _variants(finding, sources, documents, issue_id):
    """Группирует варианты с единым оформлением уровня и проверяет их контрактом."""
    field = _FIELDS.get(finding["code"])
    if field is None:
        return [], []
    targets = {}
    for identity, matches in sources:
        matches = [(path, row) for path, row in matches if field in row]
        if len(matches) != 1:
            return [], ["Координаты не определяют единственное изменяемое поле документа"]
        path, row = matches[0]
        targets[identity, path + "/" + field] = row[field]
    values = {}
    for value in targets.values():
        if value is None:
            continue
        normalized_value = (
            normalize_variant_formulation_text(value)
            if field in {"formulation", "text"} and isinstance(value, str)
            else value
        )
        signature = (
            digest({"text": normalized_value})
            if field in {"formulation", "text"} and isinstance(value, str)
            else digest(value)
        )
        values.setdefault(
            signature,
            format_variant_formulation_text(value)
            if field in {"formulation", "text"} and isinstance(value, str)
            else value,
        )
    if not values:
        return [], []
    # Данные разных известных программ не должны образовывать общий выбор.
    original = {str(item["document_id"]): item["parsed"] for item in documents}
    programs = {
        original[identity].get("meta", {}).get("program_id") for identity, _ in targets
    } - {None, ""}
    if len(programs) > 1:
        return [], ["Источники принадлежат разным программам"]
    variants = []
    for signature, value in sorted(values.items()):
        if finding["code"] == "competency_code_match_candidate":
            codes = {item for item in values.values() if isinstance(item, str)}
            changes = _competency_code_changes(original, codes, value)
        else:
            changes = [
                {"op": "set_document_value", "document_id": identity, "field_path": path,
                 "expected_old_value": deepcopy(old), "value": deepcopy(value)}
                for (identity, path), old in sorted(targets.items())
            ]
        try:
            validate_document_mutation_variants(original, [changes])
        except (DomainError, ValueError):
            continue
        variants.append({"id": str(uuid5(issue_id, signature)),
                         "label": f"Выбрать значение из документов: {value}",
                         "changes": changes})
    if finding["code"] == "competency_code_match_candidate":
        keep_changes = [
            {"op": "set_document_value", "document_id": identity,
             "field_path": path, "expected_old_value": deepcopy(old),
             "value": deepcopy(old)}
            for (identity, path), old in sorted(targets.items())
        ]
        try:
            validate_document_mutation_variants(original, [keep_changes])
        except (DomainError, ValueError):
            pass
        else:
            signature = digest({"decision": "keep_separate", "changes": keep_changes})
            variants.append({
                "id": str(uuid5(issue_id, signature)),
                "label": "Оставить коды разными",
                "changes": keep_changes,
            })
    return (variants, []) if variants else ([], ["Нет допустимых значений для решения"])


def _competency_code_changes(documents, codes, selected_code):
    """Согласованно меняет код компетенции и ссылки на него в документах."""
    changes = []

    def replace(document_id, path, current):
        if isinstance(current, str) and current in codes and current != selected_code:
            changes.append({
                "op": "set_document_value", "document_id": document_id,
                "field_path": path, "expected_old_value": current,
                "value": selected_code,
            })

    def walk(document_id, value, path=""):
        if isinstance(value, dict):
            for key, child in value.items():
                escaped = key.replace("~", "~0").replace("/", "~1")
                child_path = path + "/" + escaped
                if key == "competency_code":
                    replace(document_id, child_path, child)
                elif key == "code" and path.startswith("/competencies/") and (
                    len(path.split("/")) == 3
                ):
                    replace(document_id, child_path, child)
                elif key in {"src_key", "dst_key"} and (
                    value.get("src_entity" if key == "src_key" else "dst_entity")
                    == "competency"
                ):
                    replace(document_id, child_path, child)
                walk(document_id, child, child_path)
        elif isinstance(value, list):
            for index, child in enumerate(value):
                walk(document_id, child, path + "/" + str(index))

    for document_id, document in documents.items():
        walk(document_id, document)
    return changes


def _competency_link_variants(finding, documents, issue_id):
    evidence = finding["evidence"]
    rp = evidence["document_target"]
    plan = evidence["plan_targets"]
    plan_codes = sorted({code for target in plan for code in target["expected_codes"]})
    rp_codes = rp["expected_codes"]
    original = {str(item["document_id"]): item["parsed"] for item in documents}
    declared = {
        item["code"] for document in original.values()
        for item in document.get("competencies", [])
    }
    options = []
    if not evidence["shared_discipline"] and set(plan_codes) <= declared:
        options.append((f"Список из учебного плана: {', '.join(plan_codes)}", rp, plan_codes))
    if len(plan) == 1 and evidence["plan_row_is_exclusive"] and set(rp_codes) <= declared:
        options.append((f"Список из {evidence['document_label']}: {', '.join(rp_codes)}",
                        plan[0], rp_codes))
    if not options:
        return [], ["Не удалось безопасно сопоставить связи с исходными строками"]
    options.append(("Оставить расхождение в документах", rp, rp_codes))
    variants = []
    for label, target, codes in options:
        changes = [{"op": "set_competency_links", **target, "codes": codes}]
        try:
            validate_document_mutation_variants(original, [changes])
        except (DomainError, ValueError):
            continue
        variants.append({"id": str(uuid5(issue_id, digest(changes))),
                         "label": label, "changes": changes})
    return (variants, []) if len(variants) > 1 else (
        [], ["Нет применимого варианта для согласования связей"]
    )


def analyze_documents(documents, job_id, *, config=None):
    """Собирает сырой результат валидатора до проекции в AnalysisV2."""
    config = config if config is not None else load_rules()
    inputs = validation_documents(documents)
    # This response covers only document_reconciliation; graph rules are checked
    # later against the built graph and must not be required in this report.
    ruleset = {**validation_ruleset(config, inputs), "required_rules": []}
    report = run_document_rules(inputs, config, phase="document_reconciliation")
    rules = {rule["id"]: rule for rule in config["rules"]}
    coverage, issues = [], []
    for result in report["results"]:
        revision = validation_rule_revision(config, rules[result["rule_id"]])
        coverage.append({
            "rule_code": result["rule_id"], "rule_revision": revision,
            "status": result["status"], "checks": result["checks"],
            "reason": (
                "Валидатор не смог сделать однозначный вывод; требуется ручная проверка"
                if result["status"] == "needs_review"
                else "Нет выполненных сравнений: данные отсутствуют или правило неприменимо"
                if result["checks"] == 0
                else "Проверены исходные документы; нормализованный граф ещё не построен"
            ),
        })
        for index, finding in enumerate(result["findings"]):
            issue_id = uuid5(UUID(str(job_id)),
                             f"{result['rule_id']}:{index}:{digest(finding)}")
            sources, limitations = _sources(finding, documents)
            if finding["code"] == "competency_links_choice":
                variants, variant_limits = _competency_link_variants(
                    finding, documents, issue_id
                )
            else:
                variants, variant_limits = _variants(
                    finding, sources, documents, issue_id
                )
            evidence = deepcopy(finding["evidence"])
            if limitations:
                variants = []
            if limitations or variant_limits:
                evidence["document_mapping_limitations"] = limitations + variant_limits
            issues.append({
                "id": str(issue_id), "rule_code": result["rule_id"],
                "rule_revision": revision, "type": finding["code"],
                "severity": "warning",
                "status": "needs_review" if finding["status"] == "needs_review" else "open",
                "message": finding["message"], "checks": result["checks"],
                "source_document_ids": sorted({identity for identity, _ in sources}),
                "source_ref_ids": [], "evidence": evidence,
                "requires_decision": bool(variants), "variants": variants,
            })
    return {"schema_version": 2, "ruleset": ruleset, "coverage": coverage,
            "issues": issues, "artifacts": []}


