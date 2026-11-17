import itertools

from policy_validator import errors
from policy_validator.detector import detect_cycles, detect_group
from policy_validator.parser import parse_policies

from sample_gen import (scenario_conflict, scenario_cycle, scenario_clean,
                        scenario_shadow)


def _parse(rules):
    return parse_policies({"policy_version": "1.0.0", "rules": rules}).rules


def test_clean_scenario_has_no_findings():
    rules, _ = scenario_clean()
    findings = detect_group(_parse(rules))
    assert findings == {"conflicts": [], "shadows": []}


def test_conflict_detected_with_distinct_code():
    rules, _ = scenario_conflict()
    findings = detect_group(_parse(rules))
    assert findings["conflicts"] == [{
        "code": errors.E_CONFLICT,
        "rules": ["R-101", "R-102"],
        "target": "api.gateway",
    }]


def test_shadow_detected_with_distinct_code():
    rules, _ = scenario_shadow()
    findings = detect_group(_parse(rules))
    shadows = findings["shadows"]
    # R-201 (deny, ports 1-65535, prio 900) fully covers both R-202 and R-203.
    assert {tuple(s["rules"]) for s in shadows} == {
        ("R-201", "R-202"), ("R-201", "R-203")}
    assert shadows[0]["code"] == errors.E_SHADOW


def test_cycle_detected_and_canonicalized():
    rules, _ = scenario_cycle()
    cycles = detect_cycles(_parse(rules))
    assert cycles == [["R-301", "R-302", "R-303"]]


def test_findings_independent_of_declaration_order():
    rules, _ = scenario_conflict()
    rules += scenario_shadow()[0]
    baseline = detect_group(_parse(rules))
    for perm in itertools.islice(itertools.permutations(rules), 25):
        assert detect_group(_parse(list(perm))) == baseline


def test_cycles_independent_of_declaration_order():
    rules, _ = scenario_cycle()
    baseline = detect_cycles(_parse(rules))
    for perm in itertools.islice(itertools.permutations(rules), 25):
        assert detect_cycles(_parse(list(perm))) == baseline
