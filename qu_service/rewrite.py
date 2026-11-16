"""阶段四：查询改写建议生成。

三类改写：同义归一(synonym)、类目扩展(category_expand)、丢词补全(drop_complete)。
每条建议携带来源规则 ID、触发依据与预估收益分值；
按预估收益降序、同分按规则 ID 字典序排列，最多输出可配置条数。
规则引用缺失（requires 无法满足）时输出 E_RULE_MISSING 并跳过该条规则。
"""
from __future__ import annotations

from .errors import E_RULE_CONFLICT, E_RULE_MISSING
from .hashing import fingerprint

STAGE = "rewrite"


def validate_rules(rules: list[dict], dictionaries: list[dict],
                   known_rule_ids: set[str] | None = None) -> tuple[list[dict], list[dict]]:
    """校验规则引用。返回 (有效规则列表, 缺失引用记录列表)。

    requires 支持两种引用：
      - "rule:<rule_id>"     引用另一条规则
      - "dict:<type>:<entry>" 引用词典条目
    """
    if known_rule_ids is None:
        known_rule_ids = {r["rule_id"] for r in rules}
    dict_entries = {(d["type"], e) for d in dictionaries for e in d["entries"]}

    valid: list[dict] = []
    missing: list[dict] = []
    for rule in rules:
        bad_ref = None
        for ref in rule.get("requires", []):
            parts = ref.split(":", 2)
            if parts[0] == "rule" and len(parts) == 2:
                if parts[1] not in known_rule_ids:
                    bad_ref = ref
                    break
            elif parts[0] == "dict" and len(parts) == 3:
                if (parts[1], parts[2]) not in dict_entries:
                    bad_ref = ref
                    break
            else:
                bad_ref = ref
                break
        if bad_ref is None:
            valid.append(rule)
        else:
            missing.append({"code": E_RULE_MISSING, "rule_id": rule["rule_id"], "missing_ref": bad_ref})
    return valid, missing


def generate_rewrites(text: str, slots: list[dict], rules: list[dict],
                      max_rewrites: int, conflict_pairs: list[list[str]] | None = None) -> dict:
    suggestions: list[dict] = []
    slot_types = {s["type"] for s in slots}
    category_entries = {s["entry"] for s in slots if s["type"] == "category"}

    for rule in rules:  # 规则表文件顺序遍历，确定性
        kind = rule["kind"]
        if kind == "synonym":
            pattern = rule["pattern"]
            if pattern in text:
                suggestions.append({
                    "rule_id": rule["rule_id"],
                    "kind": kind,
                    "rewritten": text.replace(pattern, rule["replacement"]),
                    "trigger": f"pattern:{pattern}",
                    "benefit": float(rule["benefit"]),
                })
        elif kind == "category_expand":
            if rule["pattern"] in category_entries:
                suggestions.append({
                    "rule_id": rule["rule_id"],
                    "kind": kind,
                    "rewritten": text.replace(rule["pattern"], rule["replacement"]),
                    "trigger": f"category_slot:{rule['pattern']}",
                    "benefit": float(rule["benefit"]),
                })
        elif kind == "drop_complete":
            trigger_slot = rule.get("trigger_slot", "brand")
            missing_slot = rule.get("missing_slot", "category")
            if trigger_slot in slot_types and missing_slot not in slot_types:
                suggestions.append({
                    "rule_id": rule["rule_id"],
                    "kind": kind,
                    "rewritten": f"{text} {rule['replacement']}",
                    "trigger": f"slot:{trigger_slot}_without_{missing_slot}",
                    "benefit": float(rule["benefit"]),
                })

    # 注入的规则冲突对：同一查询同时命中冲突对时，保留规则 ID 字典序较小者
    codes: list[str] = []
    conflict_hits: list[str] = []
    if conflict_pairs:
        by_id = {s["rule_id"]: s for s in suggestions}
        for pair in conflict_pairs:
            a, b = sorted(pair)
            if a in by_id and b in by_id:
                suggestions = [s for s in suggestions if s["rule_id"] != b]
                codes.append(E_RULE_CONFLICT)
                conflict_hits.append(f"INJ_RULE_CONFLICT:{a}|{b}")

    # 排序：预估收益降序 -> 规则 ID 字典序；最多输出可配置条数
    suggestions.sort(key=lambda s: (-s["benefit"], s["rule_id"]))
    suggestions = suggestions[:max_rewrites]

    result = {
        "suggestions": suggestions,
        "codes": codes,
        "conflict_hits": conflict_hits,
    }
    result["fingerprint"] = fingerprint(result)
    return result
