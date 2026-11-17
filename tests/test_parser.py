import pytest

from policy_validator import errors
from policy_validator.parser import parse_policies

from sample_gen import scenario_invalid


def _doc(rules):
    return {"policy_version": "1.0.0", "rules": rules}


def _rule(rid="R-1", **overrides):
    base = {
        "id": rid,
        "target": "web.frontend",
        "action": "allow",
        "condition": "port == 443",
        "priority": 100,
        "version": "1.0.0",
    }
    base.update(overrides)
    return base


def test_valid_rules_are_normalized_and_keep_raw():
    result = parse_policies(_doc([_rule("R-2"), _rule("R-1")]))
    assert not result.rejections
    assert [r.rule_id for r in result.rules] == ["R-1", "R-2"]
    assert result.rules[0].raw["condition"] == "port == 443"


def test_duplicate_id_rejected_with_code_and_index():
    result = parse_policies(_doc([_rule("R-1"), _rule("R-1"), _rule("R-2")]))
    assert len(result.rejections) == 1
    rejection = result.rejections[0]
    assert rejection.index == 1
    assert errors.E_DUPLICATE_RULE_ID in rejection.codes
    assert [r.rule_id for r in result.rules] == ["R-1", "R-2"]


@pytest.mark.parametrize("field,value,code", [
    ("target", "BAD TARGET!", errors.E_INVALID_TARGET),
    ("condition", "port === 1", errors.E_CONDITION_SYNTAX),
    ("condition", "port != 1", errors.E_FORBIDDEN_OPERATOR),
    ("condition", "not (port == 1)", errors.E_FORBIDDEN_OPERATOR),
    ("condition", "eval('1')", errors.E_FORBIDDEN_OPERATOR),
    ("condition", "unknown_field == 1", errors.E_CONDITION_SYNTAX),
    ("priority", 0, errors.E_PRIORITY_RANGE),
    ("priority", 10001, errors.E_PRIORITY_RANGE),
    ("priority", "high", errors.E_PRIORITY_RANGE),
    ("version", "v1.2", errors.E_VERSION_FORMAT),
    ("action", "block", errors.E_INVALID_ACTION),
])
def test_invalid_entries_rejected_with_business_code(field, value, code):
    result = parse_policies(_doc([_rule("R-9", **{field: value}), _rule("R-1")]))
    assert len(result.rejections) == 1
    assert result.rejections[0].index == 0
    assert code in result.rejections[0].codes
    # Processing continues with the remaining entries.
    assert [r.rule_id for r in result.rules] == ["R-1"]


def test_missing_fields_rejected():
    result = parse_policies(_doc([{"id": "R-1"}]))
    assert result.rejections
    assert errors.E_MISSING_FIELD in result.rejections[0].codes


def test_invalid_scenario_sample_codes():
    rules, _ = scenario_invalid()
    result = parse_policies(_doc(rules))
    by_id = {r.rule_id: r for r in result.rejections}
    assert errors.E_DUPLICATE_RULE_ID in by_id["R-801"].codes
    assert errors.E_INVALID_TARGET in by_id["R-802"].codes
    assert errors.E_CONDITION_SYNTAX in by_id["R-803"].codes
    assert errors.E_FORBIDDEN_OPERATOR in by_id["R-804"].codes
    assert errors.E_PRIORITY_RANGE in by_id["R-805"].codes
    assert errors.E_VERSION_FORMAT in by_id["R-806"].codes
    assert sorted(r.rule_id for r in result.rules) == ["R-801", "R-807"]
