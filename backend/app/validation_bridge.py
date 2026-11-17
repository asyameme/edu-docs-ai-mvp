"""Связь локального валидатора с контрактом результатов Backend."""

import hashlib
import json
import math
import time
from collections.abc import Mapping
from copy import deepcopy
from pathlib import Path
from typing import Any
from uuid import UUID, uuid5

from app.document_reconciliation import apply_document_mutations
from app.review_logic import digest
from worker.validation.validator import Document, load_rules, run_validation

ADAPTER_REVISION = "local-validation-adapter-v1"
VALIDATION_BUDGET_SECONDS = 360
VALIDATION_MAX_LLM_CALLS = 20
VALIDATION_LLM_CALL_TIMEOUT_SECONDS = 45


def validation_implementation_revision():
    """Вычисляет отпечаток файлов исполняемого адаптера и правил проверки."""
    root = Path(__file__).resolve().parents[1]
    worker = root / "worker" / "validation"
    files = [
        Path(__file__).resolve(),
        root / "app" / "document_analysis.py",
        root / "app" / "document_reconciliation.py",
        root / "app" / "review_projection.py",
        worker / "validator.py",
        worker / "semantic_competency.py",
        worker / "semantic_llm.py",
        worker / "semantic_indicators.py",
        worker / "semantic_rpd.py",
    ]
    return digest(
        {
            "adapter_revision": ADAPTER_REVISION,
            "files": {
                str(path.relative_to(root)): hashlib.sha256(path.read_bytes()).hexdigest()
                for path in files
            },
        }
    )


class _ValidationBudget:
    """Ограничивает все семантические запросы одной проверки общим бюджетом."""

    def __init__(self):
        self.deadline = time.monotonic() + VALIDATION_BUDGET_SECONDS
        self.calls = 0

    def evaluate(self, evaluator, payload: Mapping[str, Any], config: Mapping[str, Any]):
        remaining = self.deadline - time.monotonic()
        if remaining <= 0:
            raise RuntimeError("Исчерпан общий бюджет времени семантических проверок")
        if self.calls >= VALIDATION_MAX_LLM_CALLS:
            raise RuntimeError("Исчерпан лимит запросов семантических проверок")
        self.calls += 1
        request_config = {
            **config,
            "timeout_seconds": min(
                float(config.get("timeout_seconds", VALIDATION_LLM_CALL_TIMEOUT_SECONDS)),
                VALIDATION_LLM_CALL_TIMEOUT_SECONDS,
                remaining,
            ),
        }
        return evaluator(payload, request_config)

    def semantic(self, payload: Mapping[str, Any], config: Mapping[str, Any]):
        from worker.validation.semantic_llm import evaluate_semantic_payload

        options = dict(config)
        system_prompt = options.pop("_system_prompt", "")
        return self.evaluate(
            lambda body, options: evaluate_semantic_payload(
                body,
                options,
                system_prompt=system_prompt,
                evaluator=None,
            ),
            payload,
            options,
        )

    def competency(self, payload: Mapping[str, Any], config: Mapping[str, Any]):
        from worker.validation.semantic_competency import _model_evaluator

        return self.evaluate(_model_evaluator, payload, config)


def project_snapshot_documents(documents, snapshot):
    """Переносит простые изменения атрибутов в JSON для повторной сверки."""
    data = snapshot.model_dump(mode="python")
    outputs = {str(item["document_id"]): deepcopy(item["parsed"]) for item in documents}
    targets = {("entity", str(item["id"])): item for item in data["entities"]}
    targets.update({("relation", str(item["id"])): item for item in data["relations"]})
    refs = {str(item["id"]): item for item in data["source_refs"]}
    source_links = {
        (item["target_kind"], str(item["target_id"]), item["field_path"]): []
        for item in data["source_links"]
    }
    for item in data["source_links"]:
        source_links[(item["target_kind"], str(item["target_id"]), item["field_path"])].append(item)

    document_changes = []
    if snapshot.review:
        for decision in snapshot.review.decisions:
            for change in decision.changes:
                if change.get("op") == "set_document_value":
                    document_changes.append(change)
                    continue
                if change.get("op") != "set_attribute":
                    raise ValueError(
                        "Повторная проверка пока не поддерживает структурные изменения"
                    )
                key = (change["target_kind"], str(change["target_id"]), change["field_path"])
                if key not in source_links:
                    raise ValueError("Нет исходного поля для поддержанной повторной проверки")
                supported_fields = {"/attributes/hours_total", "/attributes/credit_units"}
                if (
                    change["target_kind"] != "entity"
                    or change["field_path"] not in supported_fields
                ):
                    raise ValueError(
                        "Повторная проверка поддерживает только часы и зачётные единицы"
                    )

    outputs = apply_document_mutations(outputs, document_changes)

    def same_locator(ref, locator):
        # Нормализованный граф дополняет координаты null-полями схемы.
        return isinstance(ref, dict) and {
            key: item for key, item in ref.items() if item is not None
        } == {key: item for key, item in locator.items() if item is not None}

    def update_record(value, locator, field, new_value):
        if isinstance(value, dict):
            if same_locator(value.get("source_ref"), locator):
                if field not in value:
                    raise ValueError(f"В исходных данных нет поля {field}")
                value[field] = new_value
                return 1
            return sum(
                update_record(child, locator, field, new_value)
                for key, child in value.items()
                if key not in {"meta", "parse_issues", "source_ref"}
            )
        if isinstance(value, list):
            return sum(update_record(child, locator, field, new_value) for child in value)
        return 0

    def record_values(value, locator, field):
        if isinstance(value, dict):
            if same_locator(value.get("source_ref"), locator):
                return [value[field]] if field in value else []
            return [
                found
                for key, child in value.items()
                if key not in {"meta", "parse_issues", "source_ref"}
                for found in record_values(child, locator, field)
            ]
        if isinstance(value, list):
            return [
                found
                for child in value
                for found in record_values(child, locator, field)
            ]
        return []

    for link in data["source_links"]:
        if link["field_path"] == "$":
            continue
        target = targets.get((link["target_kind"], str(link["target_id"])))
        ref = refs[str(link["source_ref_id"])]
        document = outputs.get(str(ref["document_id"]))
        if target is None or document is None:
            raise ValueError("Не удалось восстановить исходное поле для повторной проверки")
        old_value = link["observed_value"].get("value")
        new_value = target.get("attributes", {}).get(link["field_path"].split("/")[-1])
        parts = link["field_path"].split("/")
        supported_fields = {"/attributes/hours_total", "/attributes/credit_units"}
        is_supported = (
            link["target_kind"] == "entity"
            and len(parts) == 3
            and parts[1] == "attributes"
            and link["field_path"] in supported_fields
        )
        field = parts[2].replace("~1", "/").replace("~0", "~") if len(parts) > 2 else ""
        try:
            locator = json.loads(ref["locator"])
        except json.JSONDecodeError as error:
            raise ValueError("Источник не содержит координаты для повторной проверки") from error
        if not is_supported:
            unchanged = old_value == new_value and isinstance(old_value, bool) == isinstance(
                new_value, bool
            )
            if unchanged:
                continue
            values = record_values(document, locator, field)
            if len(values) == 1 and values[0] == new_value and type(values[0]) is type(new_value):
                continue
            raise ValueError("Повторная проверка поддерживает только часы и зачётные единицы")
        if old_value is None and new_value is None:
            # Неизвестное исходное число остаётся неизвестным, это не правка пользователя.
            continue
        if (
            isinstance(new_value, bool)
            or not isinstance(new_value, (int, float))
            or not math.isfinite(new_value)
        ):
            raise ValueError("Часы и зачётные единицы должны иметь конечное числовое значение")
        count = update_record(document, locator, field, new_value)
        if count != 1:
            raise ValueError("Источник поля неоднозначен для повторной проверки")
    return [
        {**item, "parsed": outputs[str(item["document_id"])]} for item in documents
    ]


def validation_rule_revision(config, rule):
    """Сохраняет заданную ревизию; хэш конфигурации нужен лишь при её отсутствии."""
    return str(rule.get("revision") or config.get("revision") or digest(rule))


def validation_ruleset(config, documents):
    available = {item.role for item in documents if item.role}
    enabled = [rule for rule in config["rules"] if rule.get("status") == "ready"]
    required_documents = sorted(available)
    if not required_documents:
        raise ValueError("Нет документов поддерживаемых типов для проверки")
    return {
        "id": "education-backend-validation",
        "revision": digest(
            {
                "implementation": validation_implementation_revision(),
                "config": config,
                "required_document_types": required_documents,
            }
        ),
        "config": config,
        "required_rules": [rule["id"] for rule in enabled],
        "required_document_types": required_documents,
    }


def validation_documents(documents):
    """Добавляет служебную идентичность в копии входов, сохраняя оригиналы."""
    def attach_identity(value, document_id):
        if isinstance(value, dict):
            for child in value.values():
                attach_identity(child, document_id)
            ref = value.get("source_ref")
            if isinstance(ref, dict):
                ref["document_id"] = document_id
        elif isinstance(value, list):
            for child in value:
                attach_identity(child, document_id)

    inputs = []
    for item in documents:
        parsed = deepcopy(item["parsed"])
        identity = str(item["document_id"])
        attach_identity(parsed, identity)
        meta = parsed.get("meta", {})
        original_name = meta.get("file_name") if isinstance(meta, dict) else None
        label = original_name if isinstance(original_name, str) and original_name else identity
        inputs.append(Document(parsed, label, identity))
    return inputs


def run_document_rules(inputs, config, *, phase):
    """Запускает общий движок с тем же профилем и бюджетом на указанной стадии."""
    active_config = {
        **config,
        "rules": [
            rule for rule in config["rules"]
            if rule.get("status") in {"ready", "ready_with_semantic_fallback"}
        ],
    }
    budget = _ValidationBudget()
    return run_validation(
        inputs, active_config, phase=phase,
        semantic_evaluator=budget.semantic, competency_evaluator=budget.competency,
    ).to_dict()


def run_local_check(documents, snapshot, job_id, *, config=None, ruleset=None):
    """Проверяет сохранённые документы и связывает замечания с их источниками."""
    config = config or load_rules()
    inputs = validation_documents(documents)
    ruleset = ruleset or validation_ruleset(config, inputs)
    if ruleset.get("id") == "education-backend-validation":
        expected = validation_ruleset(config, inputs)
        if ruleset.get("revision") != expected["revision"]:
            raise ValueError(
                "Профиль или ревизия локального валидатора несовместима с сохранённым заданием"
            )
    report = run_document_rules(inputs, config, phase="graph_validation")
    refs = {}
    document_aliases = {}
    for document in inputs:
        parsed = document.data
        meta = parsed.get("meta", {})
        aliases = {document.document_id}
        if isinstance(meta, dict) and isinstance(meta.get("file_name"), str):
            aliases.add(meta["file_name"])
        for alias in aliases:
            document_aliases.setdefault(alias, set()).add(document.document_id)
    for ref in snapshot.source_refs:
        try:
            locator = json.loads(ref.locator)
        except (TypeError, json.JSONDecodeError):
            continue
        locator.pop("file", None)
        ref_key = (
            str(ref.document_id),
            json.dumps(locator, sort_keys=True, ensure_ascii=False, separators=(",", ":")),
        )
        refs.setdefault(ref_key, set()).add(str(ref.id))
    coverage, issues = [], []
    result_by_id = {item["id"]: item for item in config["rules"]}
    for result in report["results"]:
        findings = result["findings"]
        status = result["status"]
        coverage_status = {
            "passed": "passed",
            "failed": "failed",
            "needs_review": "needs_review",
            "skipped": "skipped",
        }[status]
        rule_revision = validation_rule_revision(config, result_by_id[result["rule_id"]])
        coverage.append(
            {
                "rule_code": result["rule_id"],
                "rule_revision": rule_revision,
                "status": coverage_status,
                "checks": result["checks"],
                "reason": (
                    "Нет выполненных сравнений: нужные данные отсутствуют или правило неприменимо"
                    if status == "skipped"
                    else "Валидатор не смог сделать однозначный вывод; требуется ручная проверка"
                    if status == "needs_review"
                    else "Учтены выполненные сравнения; "
                    "полнота проверки всех записей не гарантируется"
                ),
            }
        )
        for index, finding in enumerate(findings):
            source_ids = []
            source_mapping_limitations = []
            for evidence in finding["evidence"].get("source_refs", []):
                file_alias = evidence.get("file")
                evidence_document_id = evidence.get("document_id")
                locator = {
                    key: value
                    for key, value in evidence.items()
                    if key not in {"file", "document_id"}
                }
                locator_key = json.dumps(
                    locator, sort_keys=True, ensure_ascii=False, separators=(",", ":")
                )
                document_ids = (
                    {str(evidence_document_id)}
                    if evidence_document_id is not None
                    else document_aliases.get(str(file_alias), set())
                )
                matched_refs = set().union(
                    *(refs.get((document_id, locator_key), set()) for document_id in document_ids)
                )
                if len(matched_refs) == 1:
                    ref_id = matched_refs.pop()
                else:
                    ref_id = None
                    source_label = file_alias or evidence_document_id
                    if len(matched_refs) > 1:
                        source_mapping_limitations.append(
                            f"Источник {source_label} неоднозначен по координатам"
                        )
                    else:
                        source_mapping_limitations.append(
                            f"Не найден точный источник {source_label} по координатам"
                        )
                if ref_id is not None and ref_id not in source_ids:
                    source_ids.append(ref_id)
            evidence = finding["evidence"]
            if source_mapping_limitations:
                evidence["source_mapping_limitations"] = source_mapping_limitations
            issue_id = uuid5(
                UUID(str(job_id)),
                f"{result['rule_id']}:{finding['code']}:{index}:{digest(finding['evidence'])}",
            )
            issues.append(
                {
                    "id": str(issue_id),
                    "rule_code": result["rule_id"],
                    "rule_revision": rule_revision,
                    "type": finding["code"],
                    "severity": "blocking" if finding["status"] == "failed" else "warning",
                    "status": "needs_review" if finding["status"] == "needs_review" else "open",
                    "message": finding["message"],
                    "source_ref_ids": source_ids,
                    "requires_decision": False,
                    "variants": [],
                    "evidence": evidence,
                    "checks": result["checks"],
                }
            )
    return {"ruleset": ruleset, "coverage": coverage, "issues": issues}
