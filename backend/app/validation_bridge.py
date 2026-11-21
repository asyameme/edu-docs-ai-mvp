"""Связь локального валидатора с контрактом результатов Backend."""

import hashlib
import json
import logging
import math
import threading
import time
from collections.abc import Mapping
from copy import deepcopy
from pathlib import Path
from typing import Any
from uuid import UUID, uuid5

from app.confirmation_policy import CONFIRMATION_POLICY
from app.document_reconciliation import apply_document_mutations
from app.parser_document import parse_output
from app.review_logic import digest
from worker.validation.validator import Document, load_rules, run_validation

ADAPTER_REVISION = "local-validation-adapter-v1"
VALIDATION_BUDGET_SECONDS = 3500
# Leave enough headroom for every semantic rule in a full package run. The
# shared wall-clock budget and request pacing remain the effective limits.
VALIDATION_MAX_LLM_CALLS = 600
VALIDATION_LLM_CALL_TIMEOUT_SECONDS = 180
logger = logging.getLogger(__name__)
_LLM_RATE_LIMIT_LOCK = threading.Lock()
_LLM_REQUEST_SEMAPHORE = threading.Semaphore(1)
_NEXT_LLM_REQUEST_AT = 0.0


def _is_rate_limit_error(error: Exception) -> bool:
    status_code = getattr(error, "status_code", None)
    if status_code is None:
        status_code = getattr(getattr(error, "response", None), "status_code", None)
    if status_code == 429:
        return True
    try:
        from litellm.exceptions import RateLimitError
    except ImportError:
        return False
    return isinstance(error, RateLimitError)


def _is_retryable_semantic_error(error: Exception) -> bool:
    try:
        from worker.validation.semantic_llm import SemanticEvaluationUnavailable
    except ImportError:
        return False
    if not isinstance(error, SemanticEvaluationUnavailable):
        return False
    message = str(error).lower()
    return any(
        marker in message
        for marker in (
            "не удалось разобрать как json",
            "не в текстовом формате",
            "json не в виде объекта",
            "завершился ошибкой (timeout",
            "завершился ошибкой (connect",
            "завершился ошибкой (readtimeout",
            "завершился ошибкой (internalserver",
            "завершился ошибкой (serviceunavailable",
            "завершился ошибкой (apiconnection",
        )
    )


def _rate_limit_retry_delay(error: Exception, config: Mapping[str, Any], retry_index: int) -> float | None:
    base_delay = config.get("rate_limit_retry_delay_seconds", 5)
    if (
        not isinstance(base_delay, (int, float))
        or isinstance(base_delay, bool)
        or not math.isfinite(base_delay)
        or base_delay <= 0
    ):
        base_delay = 5.0
    max_delay = config.get("rate_limit_max_retry_delay_seconds", 300)
    if (
        not isinstance(max_delay, (int, float))
        or isinstance(max_delay, bool)
        or not math.isfinite(max_delay)
        or max_delay < base_delay
    ):
        max_delay = 300.0

    retry_after = _retry_after_seconds(error) or 0.0
    backoff = min(float(base_delay) * (2 ** retry_index), float(max_delay))
    return max(backoff, retry_after)


def _retry_after_seconds(error: Exception) -> float | None:
    direct_value = getattr(error, "retry_after_seconds", None)
    if isinstance(direct_value, (int, float)) and not isinstance(direct_value, bool):
        if math.isfinite(direct_value) and direct_value >= 0:
            return float(direct_value)
    response = getattr(error, "response", None)
    for headers in (getattr(error, "headers", None), getattr(response, "headers", None)):
        if not isinstance(headers, Mapping):
            continue
        value = next(
            (value for key, value in headers.items() if str(key).lower() == "retry-after"),
            None,
        )
        if value is None:
            continue
        try:
            seconds = float(value)
        except (TypeError, ValueError):
            continue
        if math.isfinite(seconds) and seconds >= 0:
            return seconds
    return None


def _wait_for_llm_request_slot(deadline: float, config: Mapping[str, Any]) -> None:
    global _NEXT_LLM_REQUEST_AT
    interval = config.get("minimum_request_interval_seconds", 5)
    if (
        not isinstance(interval, (int, float))
        or isinstance(interval, bool)
        or not math.isfinite(interval)
        or not 0 < interval <= 120
    ):
        interval = 5.0
    with _LLM_RATE_LIMIT_LOCK:
        wait = max(0.0, _NEXT_LLM_REQUEST_AT - time.monotonic())
        if time.monotonic() + wait >= deadline:
            raise RuntimeError("Исчерпан бюджет ожидания следующего запроса LLM")
        if wait:
            time.sleep(wait)
        _NEXT_LLM_REQUEST_AT = time.monotonic() + float(interval)


def _defer_llm_requests(seconds: float) -> None:
    global _NEXT_LLM_REQUEST_AT
    with _LLM_RATE_LIMIT_LOCK:
        _NEXT_LLM_REQUEST_AT = max(
            _NEXT_LLM_REQUEST_AT,
            time.monotonic() + seconds,
        )


def validation_implementation_revision():
    """Вычисляет отпечаток файлов исполняемого адаптера и правил проверки."""
    root = Path(__file__).resolve().parents[1]
    worker = root / "worker" / "validation"
    files = [
        Path(__file__).resolve(),
        root / "app" / "confirmation_policy.py",
        root / "app" / "document_analysis.py",
        root / "app" / "document_reconciliation.py",
        root / "app" / "review_projection.py",
        worker / "validator.py",
        worker / "semantic_coverage.py",
        worker / "semantic_llm.py",
        worker / "indicator_formulations.py",
        worker / "semantic_rpd.py",
        worker / "text_normalization.py",
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
        self.rate_limit_exhausted = False

    def evaluate(self, evaluator, payload: Mapping[str, Any], config: Mapping[str, Any]):
        if self.rate_limit_exhausted:
            from worker.validation.semantic_llm import SemanticEvaluationUnavailable

            raise SemanticEvaluationUnavailable(
                "Провайдер модели продолжает отвечать HTTP 429; остальные вызовы "
                "этой проверки пропущены. Запустите проверку позже."
            )
        retries = config.get("rate_limit_retries", 5)
        if not isinstance(retries, int) or isinstance(retries, bool) or not 0 <= retries <= 8:
            retries = 5

        rate_retry_index = 0
        semantic_retry_index = 0
        remaining = self.deadline - time.monotonic()
        if remaining <= 0:
            raise RuntimeError("Исчерпан общий бюджет времени семантических проверок")
        if not _LLM_REQUEST_SEMAPHORE.acquire(timeout=remaining):
            raise RuntimeError("Исчерпан бюджет ожидания очереди запросов LLM")
        try:
            while True:
                remaining = self.deadline - time.monotonic()
                if remaining <= 0:
                    raise RuntimeError("Исчерпан общий бюджет времени семантических проверок")
                if self.calls >= VALIDATION_MAX_LLM_CALLS:
                    raise RuntimeError("Исчерпан лимит запросов семантических проверок")
                _wait_for_llm_request_slot(self.deadline, config)
                remaining = self.deadline - time.monotonic()
                if remaining <= 0:
                    raise RuntimeError("Исчерпан общий бюджет времени семантических проверок")
                self.calls += 1
                request_config = {
                    **config,
                    "timeout_seconds": min(
                        float(config.get("timeout_seconds", VALIDATION_LLM_CALL_TIMEOUT_SECONDS)),
                        VALIDATION_LLM_CALL_TIMEOUT_SECONDS,
                        remaining,
                    ),
                }
                try:
                    return evaluator(payload, request_config)
                except Exception as exc:
                    if _is_rate_limit_error(exc):
                        if rate_retry_index >= retries:
                            self.rate_limit_exhausted = True
                            raise
                        delay = _rate_limit_retry_delay(exc, config, rate_retry_index)
                        retry_kind = "rate_limit"
                        rate_retry_index += 1
                        _defer_llm_requests(delay or 0.0)
                    elif _is_retryable_semantic_error(exc):
                        semantic_retries = config.get("semantic_retries", 1)
                        if (
                            not isinstance(semantic_retries, int)
                            or isinstance(semantic_retries, bool)
                            or not 0 <= semantic_retries <= 2
                        ):
                            semantic_retries = 1
                        if semantic_retry_index >= semantic_retries:
                            raise
                        delay = config.get("semantic_retry_delay_seconds", 1)
                        if (
                            not isinstance(delay, (int, float))
                            or isinstance(delay, bool)
                            or not math.isfinite(delay)
                            or delay < 0
                        ):
                            delay = 1.0
                        retry_kind = "semantic_response"
                        semantic_retry_index += 1
                    else:
                        raise
                    remaining = self.deadline - time.monotonic()
                    if (
                        delay is None
                        or remaining <= delay
                        or self.calls >= VALIDATION_MAX_LLM_CALLS
                    ):
                        raise
                    logger.warning(
                        "Retrying semantic LLM request",
                        extra={
                            "retry_kind": retry_kind,
                            "retry_index": rate_retry_index if retry_kind == "rate_limit" else semantic_retry_index,
                            "retry_delay_seconds": delay,
                            "rate_limit_type": getattr(exc, "rate_limit_type", None),
                        },
                    )
                    time.sleep(delay)
        finally:
            _LLM_REQUEST_SEMAPHORE.release()

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

def project_snapshot_documents(documents, snapshot):
    """Переносит изменения атрибутов по источникам в JSON для повторной сверки."""
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
    review_attribute_fields = set()
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
                if not change["field_path"].startswith("/attributes/"):
                    raise ValueError("Изменяемый путь должен находиться внутри attributes")
                review_attribute_fields.add(key)
    else:
        # Старые версии без review сохраняют прежнее поведение только для чисел.
        for link in data["source_links"]:
            if link["field_path"] not in {
                "/attributes/hours_total", "/attributes/credit_units"
            }:
                continue
            target = targets.get((link["target_kind"], str(link["target_id"])))
            if target is None:
                continue
            field = link["field_path"].removeprefix("/attributes/")
            old_value = link["observed_value"].get("value")
            new_value = target.get("attributes", {}).get(field)
            if old_value != new_value or type(old_value) is not type(new_value):
                review_attribute_fields.add(
                    (link["target_kind"], str(link["target_id"]), link["field_path"])
                )
        if not review_attribute_fields:
            return [
                {**item, "parsed": outputs[str(item["document_id"])]} for item in documents
            ]

    outputs = apply_document_mutations(outputs, document_changes)

    def same_locator(ref, locator):
        # Нормализованный граф дополняет координаты null-полями схемы.
        return isinstance(ref, dict) and {
            key: item for key, item in ref.items() if item is not None
        } == {key: item for key, item in locator.items() if item is not None}

    def pointer_tokens(pointer):
        if not isinstance(pointer, str) or not pointer.startswith("/"):
            raise ValueError("Изменяемое поле должно быть задано JSON Pointer")
        return [part.replace("~1", "/").replace("~0", "~") for part in pointer[1:].split("/")]

    def pointer_get(value, tokens):
        for token in tokens:
            if isinstance(value, list):
                if (
                    not token.isascii()
                    or not token.isdigit()
                    or len(token) > 1 and token.startswith("0")
                    or int(token) >= len(value)
                ):
                    raise KeyError(token)
                value = value[int(token)]
            elif isinstance(value, dict) and token in value:
                value = value[token]
            else:
                raise KeyError(token)
        return value

    def pointer_set(value, tokens, new_value):
        if not tokens:
            raise ValueError("Нельзя заменить исходный объект целиком")
        parent = pointer_get(value, tokens[:-1])
        last = tokens[-1]
        if isinstance(parent, list):
            if (
                not last.isascii()
                or not last.isdigit()
                or len(last) > 1 and last.startswith("0")
                or int(last) >= len(parent)
            ):
                raise KeyError(last)
            parent[int(last)] = deepcopy(new_value)
        elif isinstance(parent, dict) and last in parent:
            parent[last] = deepcopy(new_value)
        else:
            raise KeyError(last)

    def update_record(value, locator, field_tokens, new_value):
        if isinstance(value, dict):
            if same_locator(value.get("source_ref"), locator):
                try:
                    pointer_set(value, field_tokens, new_value)
                except KeyError:
                    return 0
                return 1
            return sum(
                update_record(child, locator, field_tokens, new_value)
                for key, child in value.items()
                if key not in {"meta", "parse_issues", "source_ref"}
            )
        if isinstance(value, list):
            return sum(update_record(child, locator, field_tokens, new_value) for child in value)
        return 0

    def record_values(value, locator, field_tokens):
        if isinstance(value, dict):
            if same_locator(value.get("source_ref"), locator):
                try:
                    return [pointer_get(value, field_tokens)]
                except KeyError:
                    return []
            return [
                found
                for key, child in value.items()
                if key not in {"meta", "parse_issues", "source_ref"}
                for found in record_values(child, locator, field_tokens)
            ]
        if isinstance(value, list):
            return [
                found
                for child in value
                for found in record_values(child, locator, field_tokens)
            ]
        return []

    for link in data["source_links"]:
        if link["field_path"] == "$":
            continue
        key = (link["target_kind"], str(link["target_id"]), link["field_path"])
        if key not in review_attribute_fields:
            continue
        target = targets.get((link["target_kind"], str(link["target_id"])))
        ref = refs[str(link["source_ref_id"])]
        document = outputs.get(str(ref["document_id"]))
        if target is None or document is None:
            raise ValueError("Не удалось восстановить исходное поле для повторной проверки")
        path_tokens = pointer_tokens(link["field_path"])
        if len(path_tokens) < 2 or path_tokens[0] != "attributes":
            raise ValueError("Исходное поле должно находиться внутри attributes")
        if any(token in {"meta", "source_ref", "parse_issues"} for token in path_tokens):
            raise ValueError("Нельзя переносить изменения в метаданные или свидетельства")
        try:
            new_value = pointer_get(target.get("attributes", {}), path_tokens[1:])
        except KeyError as error:
            raise ValueError("Изменённое поле отсутствует в графе") from error
        try:
            locator = json.loads(ref["locator"])
        except json.JSONDecodeError as error:
            raise ValueError("Источник не содержит координаты для повторной проверки") from error
        field_tokens = path_tokens[1:]
        values = record_values(document, locator, field_tokens)
        if len(values) == 1 and values[0] == new_value and type(values[0]) is type(new_value):
            continue
        numeric_field = len(field_tokens) == 1 and field_tokens[0] in {
            "hours_total", "credit_units"
        }
        if not numeric_field or new_value is None:
            count = update_record(document, locator, field_tokens, new_value)
            if count != 1:
                raise ValueError("Источник поля неоднозначен для повторной сверки")
            continue
        if (
            isinstance(new_value, bool)
            or not isinstance(new_value, (int, float))
            or not math.isfinite(new_value)
        ):
            raise ValueError("Часы и зачётные единицы должны иметь конечное числовое значение")
        count = update_record(document, locator, field_tokens, new_value)
        if count != 1:
            raise ValueError("Источник поля неоднозначен для повторной проверки")
    projected = [
        {**item, "parsed": outputs[str(item["document_id"])]} for item in documents
    ]
    try:
        for item in projected:
            parse_output(item["parsed"])
    except (TypeError, ValueError) as error:
        raise ValueError("Изменение нарушает контракт исходного документа") from error
    return projected


def validation_rule_revision(config, rule):
    """Сохраняет заданную ревизию; хэш конфигурации нужен лишь при её отсутствии."""
    return str(rule.get("revision") or config.get("revision") or digest(rule))


def required_validation_rules(config, phase):
    """Return required automatic rules for one stage, in configuration order."""
    return [
        rule["id"]
        for rule in config["rules"]
        if rule.get("status") in {"ready", "ready_with_semantic_fallback"}
        and phase in rule.get("phases", [])
        and rule.get("run_mode", "automatic") == "automatic"
        and rule.get(
            "required_for_confirmation",
            rule.get("status") == "ready" and rule.get("run_mode", "automatic") == "automatic",
        )
    ]


def validation_ruleset(config, documents):
    available = {item.role for item in documents if item.role}
    required_rules = required_validation_rules(config, "graph_validation")
    required_documents = sorted(available)
    if not required_documents:
        raise ValueError("Нет документов поддерживаемых типов для проверки")
    return {
        "id": "education-backend-validation",
        "revision": CONFIRMATION_POLICY["revision"] + ":" + digest(
            {
                "implementation": validation_implementation_revision(),
                "config": config,
                "required_document_types": required_documents,
                "confirmation_policy": CONFIRMATION_POLICY,
            }
        ),
        "config": config,
        "required_rules": required_rules,
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


def run_document_rules(inputs, config, *, phase, rule_codes=None):
    """Запускает общий движок с тем же профилем и бюджетом на указанной стадии."""
    budget = _ValidationBudget()
    return run_validation(
        inputs, config, phase=phase,
        semantic_evaluator=budget.semantic,
        rule_codes=rule_codes,
    ).to_dict()


def run_local_check(documents, snapshot, job_id, *, config=None, ruleset=None, rule_codes=None):
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
    if rule_codes is not None:
        ruleset = {**ruleset, "required_rules": []}
    report = run_document_rules(
        inputs, config, phase="graph_validation", rule_codes=rule_codes
    )
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
                    "severity": "warning",
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
