from qu_service.errors import E_RULE_CONFLICT, E_RULE_MISSING
from qu_service.rewrite import generate_rewrites, validate_rules

DICTS = [{"type": "category", "priority": 2, "entries": ["手机", "耳机"]}]


def _rules():
    return [
        {"rule_id": "R_A", "kind": "synonym", "pattern": "购置", "replacement": "买",
         "benefit": 0.6, "requires": []},
        {"rule_id": "R_B", "kind": "synonym", "pattern": "购置", "replacement": "购买",
         "benefit": 0.6, "requires": []},
        {"rule_id": "R_C", "kind": "category_expand", "pattern": "手机", "replacement": "智能手机",
         "benefit": 0.9, "requires": ["dict:category:手机"]},
        {"rule_id": "R_D", "kind": "drop_complete", "trigger_slot": "brand",
         "missing_slot": "category", "replacement": "手机", "benefit": 0.8, "requires": []},
    ]


def test_ordering_benefit_desc_then_rule_id():
    slots = [{"type": "category", "entry": "手机"}]
    result = generate_rewrites("购置手机", slots, _rules(), max_rewrites=10)
    ids = [s["rule_id"] for s in result["suggestions"]]
    assert ids == ["R_C", "R_A", "R_B"]  # 0.9 优先，同分 0.6 按规则 ID 字典序


def test_max_rewrites_limit():
    slots = [{"type": "category", "entry": "手机"}]
    result = generate_rewrites("购置手机", slots, _rules(), max_rewrites=2)
    assert len(result["suggestions"]) == 2


def test_drop_complete_trigger():
    slots = [{"type": "brand", "entry": "苹果"}]
    result = generate_rewrites("苹果", slots, _rules(), max_rewrites=5)
    assert result["suggestions"][0]["rule_id"] == "R_D"
    assert result["suggestions"][0]["rewritten"] == "苹果 手机"


def test_rule_missing_reference_skipped():
    rules = _rules() + [{"rule_id": "R_BAD", "kind": "synonym", "pattern": "购置",
                         "replacement": "x", "benefit": 1.0,
                         "requires": ["dict:category:不存在"]}]
    valid, missing = validate_rules(rules, DICTS)
    assert [m["rule_id"] for m in missing] == ["R_BAD"]
    assert missing[0]["code"] == E_RULE_MISSING
    assert all(r["rule_id"] != "R_BAD" for r in valid)


def test_rule_conflict_resolution():
    rules = [
        {"rule_id": "R_X", "kind": "synonym", "pattern": "移动电话", "replacement": "手机",
         "benefit": 0.7, "requires": []},
        {"rule_id": "R_Y", "kind": "synonym", "pattern": "移动电话", "replacement": "移动手机",
         "benefit": 0.9, "requires": []},
    ]
    result = generate_rewrites("移动电话", [], rules, max_rewrites=5,
                               conflict_pairs=[["R_Y", "R_X"]])
    ids = [s["rule_id"] for s in result["suggestions"]]
    assert ids == ["R_X"]  # 冲突对保留字典序较小者，与收益无关
    assert E_RULE_CONFLICT in result["codes"]
    assert result["conflict_hits"] == ["INJ_RULE_CONFLICT:R_X|R_Y"]
