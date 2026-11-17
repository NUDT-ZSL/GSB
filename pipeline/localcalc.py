"""管线侧独立结论计算（业务要求 2/4 的对照方）。

采用逐对区间求交的经典写法，与服务端（segment 分解）实现互相独立，
不得被服务端复用。
"""

from __future__ import annotations


def _contains(interval, point):
    lower, upper, lower_closed, upper_closed = interval
    if point < lower or point > upper:
        return False
    if point == lower and not lower_closed:
        return False
    if point == upper and not upper_closed:
        return False
    return True


def _overlaps(a, b):
    lower = max(a[0], b[0])
    upper = min(a[1], b[1])
    if lower < upper:
        return True
    if lower > upper:
        return False
    # 仅在一点相接：要求该点对两个区间均为包含点。
    return _contains(a, lower) and _contains(b, lower)


def _covers(a, b):
    """区间 a 是否完全覆盖区间 b（含端点开闭语义）。"""
    if a[0] > b[0] or a[1] < b[1]:
        return False
    if a[0] == b[0] and b[2] and not a[2]:
        return False
    if a[1] == b[1] and b[3] and not a[3]:
        return False
    return True


def compute_summary(group, policy_version):
    """计算规则组结论摘要：冲突对与遮蔽关系。"""
    rules = sorted(group["rules"], key=lambda r: r["rule_id"])
    conflicts = []
    for i in range(len(rules)):
        for j in range(i + 1, len(rules)):
            a, b = rules[i], rules[j]
            if a["action"] != b["action"] and _overlaps(a["interval"], b["interval"]):
                conflicts.append([a["rule_id"], b["rule_id"]])
    shadowed = []
    for b in rules:
        for a in rules:
            if a["rule_id"] == b["rule_id"]:
                continue
            if (a["priority"] > b["priority"]
                    and _covers(a["interval"], b["interval"])):
                shadowed.append({"rule": b["rule_id"], "by": a["rule_id"]})
    shadowed.sort(key=lambda rel: (rel["rule"], rel["by"]))
    return {
        "group_id": group["group_id"],
        "policy_version": policy_version,
        "summary_version": policy_version,
        "conflicts": conflicts,
        "shadowed": shadowed,
    }


def diff_summaries(local, received):
    """返回两份摘要之间不一致的顶层字段列表（按固定顺序）。"""
    if not isinstance(received, dict):
        return ["<summary>"]
    fields = ["group_id", "policy_version", "summary_version",
              "conflicts", "shadowed"]
    return [f for f in fields if local.get(f) != received.get(f)]
