"""解析规范化层（业务要求 1）。

逐条校验策略声明与请求清单；非法条目产出含条目序号与业务错误码的
拒绝记录并继续处理其余条目，输出规范化规则组与请求集合。
"""

from __future__ import annotations

from . import errors

# 端点标记合法取值：归一化为“是否闭端点”布尔值。
_ENDPOINT_TOKENS = {
    "inclusive": True, "closed": True, "[": True, "]": True,
    "exclusive": False, "open": False, "(": False, ")": False,
}


def _parse_endpoint(value):
    """返回 True/False（闭/开），非法取值返回 None。"""
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return _ENDPOINT_TOKENS.get(value)
    return None


def _is_number(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _reject(rejects, scope, index, code, detail):
    rejects.append({"scope": scope, "index": index, "code": code, "detail": detail})


def normalize_policy(decl):
    """规范化策略声明。

    返回 (groups, policy_version, rejects)：
    - groups: {group_id: {"group_id": str, "rules": [规范化规则, ...]}}
    - 规范化规则: {"rule_id", "action", "priority",
                   "interval": (lower, upper, lower_closed, upper_closed)}
    """
    rejects = []
    if not isinstance(decl, dict):
        _reject(rejects, "policy", None, errors.E_SCHEMA, "策略声明必须是 JSON 对象")
        return {}, "unknown", rejects

    version = decl.get("policy_version")
    if not isinstance(version, str) or not version:
        _reject(rejects, "policy", None, errors.E_SCHEMA,
                "policy_version 必须是非空字符串")
        version = "unknown"

    raw_groups = decl.get("rule_groups")
    if not isinstance(raw_groups, list):
        _reject(rejects, "policy", None, errors.E_SCHEMA,
                "rule_groups 必须是数组")
        raw_groups = []

    groups = {}
    for gi, group in enumerate(raw_groups):
        if not isinstance(group, dict):
            _reject(rejects, "rule_group", gi, errors.E_SCHEMA, "规则组必须是对象")
            continue
        gid = group.get("group_id")
        if not isinstance(gid, str) or not gid:
            _reject(rejects, "rule_group", gi, errors.E_SCHEMA,
                    "group_id 必须是非空字符串")
            continue
        if gid in groups:
            _reject(rejects, "rule_group", gi, errors.E_DUP_GROUP,
                    "规则组标识重复: %s" % gid)
            continue

        raw_rules = group.get("rules")
        if not isinstance(raw_rules, list):
            _reject(rejects, "rule_group", gi, errors.E_SCHEMA,
                    "rules 必须是数组")
            raw_rules = []

        rules = []
        seen_ids = set()
        for ri, rule in enumerate(raw_rules):
            index = {"group_index": gi, "rule_index": ri}
            if not isinstance(rule, dict):
                _reject(rejects, "rule", index, errors.E_SCHEMA, "规则必须是对象")
                continue
            rid = rule.get("rule_id")
            if not isinstance(rid, str) or not rid:
                _reject(rejects, "rule", index, errors.E_SCHEMA,
                        "rule_id 必须是非空字符串")
                continue
            if rid in seen_ids:
                _reject(rejects, "rule", index, errors.E_DUP_RULE,
                        "规则标识重复: %s" % rid)
                continue

            cond = rule.get("condition")
            if not isinstance(cond, dict):
                _reject(rejects, "rule", index, errors.E_SCHEMA,
                        "condition 必须是对象")
                continue
            lower, upper = cond.get("lower"), cond.get("upper")
            if not _is_number(lower) or not _is_number(upper):
                _reject(rejects, "rule", index, errors.E_SCHEMA,
                        "条件区间下界/上界必须是数值")
                continue
            if lower > upper:
                _reject(rejects, "rule", index, errors.E_RANGE,
                        "条件区间下界大于上界")
                continue
            lower_closed = _parse_endpoint(cond.get("lower_endpoint", "inclusive"))
            upper_closed = _parse_endpoint(cond.get("upper_endpoint", "inclusive"))
            if lower_closed is None or upper_closed is None:
                _reject(rejects, "rule", index, errors.E_ENDPOINT,
                        "端点标记取值非法")
                continue

            action = rule.get("action")
            if not isinstance(action, str) or not action:
                _reject(rejects, "rule", index, errors.E_ACTION,
                        "action 必须是非空字符串")
                continue

            priority = rule.get("priority")
            if not isinstance(priority, int) or isinstance(priority, bool):
                _reject(rejects, "rule", index, errors.E_PRIORITY,
                        "priority 必须是整数")
                continue

            seen_ids.add(rid)
            rules.append({
                "rule_id": rid,
                "action": action,
                "priority": priority,
                "interval": (lower, upper, lower_closed, upper_closed),
            })
        groups[gid] = {"group_id": gid, "rules": rules}
    return groups, version, rejects


def _valid_expected_summary(value):
    if not isinstance(value, dict):
        return False
    conflicts = value.get("conflicts")
    shadowed = value.get("shadowed")
    if not isinstance(conflicts, list) or not isinstance(shadowed, list):
        return False
    for pair in conflicts:
        if (not isinstance(pair, list) or len(pair) != 2
                or not all(isinstance(x, str) for x in pair)):
            return False
    for rel in shadowed:
        if (not isinstance(rel, dict)
                or not isinstance(rel.get("rule"), str)
                or not isinstance(rel.get("by"), str)):
            return False
    return True


def normalize_requests(manifest, groups):
    """规范化请求清单。

    返回 (requests, rejects)；每个请求的稳定键为 request_id。
    """
    rejects = []
    if isinstance(manifest, dict):
        raw = manifest.get("requests")
    else:
        raw = manifest
    if not isinstance(raw, list):
        _reject(rejects, "manifest", None, errors.E_SCHEMA, "请求清单必须是数组")
        raw = []

    requests = []
    seen_ids = set()
    for i, entry in enumerate(raw):
        if not isinstance(entry, dict):
            _reject(rejects, "request", i, errors.E_SCHEMA, "请求条目必须是对象")
            continue
        rid = entry.get("request_id")
        if not isinstance(rid, str) or not rid:
            _reject(rejects, "request", i, errors.E_SCHEMA,
                    "request_id 必须是非空字符串")
            continue
        if rid in seen_ids:
            _reject(rejects, "request", i, errors.E_DUP_REQUEST,
                    "请求标识重复: %s" % rid)
            continue
        gid = entry.get("group_id")
        if gid not in groups:
            _reject(rejects, "request", i, errors.E_UNKNOWN_GROUP,
                    "引用了不存在的规则组: %r" % (gid,))
            continue
        deadline = entry.get("deadline_ms")
        if (not isinstance(deadline, int) or isinstance(deadline, bool)
                or deadline <= 0):
            _reject(rejects, "request", i, errors.E_DEADLINE,
                    "期望返回时限必须是正整数毫秒")
            continue
        expected = entry.get("expected_summary")
        if not _valid_expected_summary(expected):
            _reject(rejects, "request", i, errors.E_SUMMARY,
                    "期望结论摘要格式非法")
            continue
        seen_ids.add(rid)
        requests.append({
            "request_id": rid,
            "group_id": gid,
            "deadline_ms": deadline,
            "expected_summary": expected,
        })
    return requests, rejects
