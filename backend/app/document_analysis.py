"""Документная стадия существующего валидатора в формате AnalysisV2 без графа."""

from copy import deepcopy
from uuid import UUID, uuid5

from app.document_reconciliation import validate_document_mutation_variants
from app.errors import DomainError
from app.review_logic import digest
from app.validation_bridge import (
    run_document_rules,
    validation_documents,
    validation_rule_revision,
    validation_ruleset,
)
from worker.validation.validator import load_rules

_FIELDS = {
    "curriculum_hours_mismatch": "hours_total",
    "value_mismatch": "credit_units",
    "competency_formulation_review": "formulation",
    "competency_formulation_mismatch": "formulation",
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
    for ref in finding["evidence"].get("source_refs", []):
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
        sources.append((str(item["document_id"]), matches))
    return sources, limitations


def _variants(finding, sources, documents, issue_id):
    """Предлагает только уже найденные значения и проверяет каждый вариант контрактом."""
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
    values = {digest(value): value for value in targets.values() if value is not None}
    if len(values) < 2:
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
                         "label": f"Выбрать значение из документов: {str(value)[:200]}",
                         "changes": changes})
    return (variants, []) if len(variants) >= 2 else ([], ["Нет двух допустимых вариантов"])


def analyze_documents(documents, job_id, *, config=None):
    """Собирает сырой результат валидатора до проекции в AnalysisV2."""
    config = config if config is not None else load_rules()
    inputs = validation_documents(documents)
    ruleset = validation_ruleset(config, inputs)
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
                "severity": "blocking" if finding["status"] == "failed" else "warning",
                "status": "needs_review" if finding["status"] == "needs_review" else "open",
                "message": finding["message"], "checks": result["checks"],
                "source_document_ids": sorted({identity for identity, _ in sources}),
                "source_ref_ids": [], "evidence": evidence,
                "requires_decision": bool(variants), "variants": variants,
            })
    return {"schema_version": 2, "ruleset": ruleset, "coverage": coverage,
            "issues": issues, "artifacts": []}
