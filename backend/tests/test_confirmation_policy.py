from worker.validation.validator import (
    Document, load_rules, _validate_rules_config,
    ValidationError,
)
from app.validation_bridge import validation_ruleset
import pytest


NEW_RULES = {
    "R-02", "R-03", "R-03-RPP",
    "R-05", "R-05-title", "R-06",
}

REQUIRED = {
    "R-01", "R-04", "R-08-local-reference",
}


def make_ruleset(config):
    documents = [
        Document({"meta": {"doc_type": "rpd"}}, "test")
    ]
    return validation_ruleset(config, documents)


def test_existing_confirmation_policy_preserved():
    config = load_rules()
    result = make_ruleset(config)
    assert set(result["required_rules"]) == REQUIRED


def test_new_ready_rules_are_not_required():
    config = load_rules()

    for rule in config["rules"]:
        if rule["id"] in NEW_RULES:
            assert rule["required_for_confirmation"] is False
            rule["status"] = "ready"

    result = make_ruleset(config)
    assert set(result["required_rules"]) == REQUIRED


def test_explicit_required_rule():
    config = load_rules()
    rule = next(r for r in config["rules"] if r["id"] == "R-02")

    rule["status"] = "ready"
    rule["required_for_confirmation"] = True

    result = make_ruleset(config)
    assert set(result["required_rules"]) == REQUIRED | {"R-02"}


def test_draft_rule_cannot_be_required():
    config = load_rules()
    rule = next(r for r in config["rules"] if r["id"] == "R-02")

    rule["status"] = "draft"
    rule["required_for_confirmation"] = True

    assert "R-02" not in make_ruleset(config)["required_rules"]


@pytest.mark.parametrize("value", ["false", 0, None])
def test_invalid_required_flag(value):
    config = load_rules()
    config["rules"][0]["required_for_confirmation"] = value

    with pytest.raises(ValidationError):
        _validate_rules_config(config)
