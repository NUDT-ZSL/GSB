"""本地确定性校验服务（业务要求 2）。

以规则组为查询单元返回冲突对与遮蔽关系的结论摘要。服务端结论由
独立的“端点原子段分解”算法在本地计算，不复用管线侧 localcalc 的
任何结果，以形成可核对的对照。

支持：
- 按摘要版本号区分不同策略声明版本（load_declaration 可装载多版本）；
- 注入确定性延迟档位（每组固定毫秒数）；
- 乱序返回模式（normal / reverse / shuffle，确定性地改变各组延迟，
  从而改变并发下的完成顺序）；
- 测试注入：serve_versions（按组返回旧版本摘要，模拟陈旧结论）、
  corrupt（按组篡改摘要，模拟不一致）。
"""

from __future__ import annotations

import random
import threading
import time

_ENDPOINT_TOKENS = {
    "inclusive": True, "closed": True, "[": True, "]": True,
    "exclusive": False, "open": False, "(": False, ")": False,
}


def _svc_endpoint(value):
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return _ENDPOINT_TOKENS.get(value, True)
    return True


class LocalPolicyService:
    """进程内本地校验服务。线程安全，可注入确定性延迟与乱序模式。"""

    def __init__(self, delay_tiers_ms=None, order_mode="normal",
                 order_step_ms=5, serve_versions=None, corrupt=None,
                 summary_version="svc-1", seed=27):
        if order_mode not in ("normal", "reverse", "shuffle"):
            raise ValueError("未知乱序返回模式: %r" % (order_mode,))
        self._delay_tiers = dict(delay_tiers_ms or {})
        self._order_mode = order_mode
        self._order_step = int(order_step_ms)
        self._serve_versions = dict(serve_versions or {})
        self._corrupt = dict(corrupt or {})
        self._summary_version = summary_version
        self._seed = seed
        self._policies = {}
        self._log = []
        self._lock = threading.Lock()

    def load_declaration(self, decl):
        """装载一份策略声明（服务端独立解析原始 JSON）。"""
        version = decl.get("policy_version", "unknown")
        groups = {}
        for group in decl.get("rule_groups", []):
            rules = []
            for rule in group.get("rules", []):
                cond = rule.get("condition", {})
                rules.append({
                    "rule_id": rule.get("rule_id"),
                    "action": rule.get("action"),
                    "priority": rule.get("priority"),
                    "lo": cond.get("lower"),
                    "hi": cond.get("upper"),
                    "lc": _svc_endpoint(cond.get("lower_endpoint", "inclusive")),
                    "hc": _svc_endpoint(cond.get("upper_endpoint", "inclusive")),
                })
            groups[group.get("group_id")] = rules
        self._policies[version] = groups

    @property
    def summary_version(self):
        return self._summary_version

    def delay_schedule(self, version):
        """返回 {group_id: 总延迟毫秒}，供样本生成器记录同步标记。"""
        groups = self._policies.get(version, {})
        return {gid: self._delay_for(gid, groups) for gid in groups}

    def _delay_for(self, group_id, groups=None):
        base = self._delay_tiers.get(group_id, 0)
        if self._order_mode == "normal":
            return base
        if groups is None:
            groups = next(iter(self._policies.values()), {})
        ordered = sorted(groups.keys())
        count = len(ordered)
        if self._order_mode == "reverse":
            rank = ordered.index(group_id) if group_id in ordered else 0
            return base + (count - 1 - rank) * self._order_step
        perm = list(ordered)
        random.Random(self._seed).shuffle(perm)
        rank = perm.index(group_id) if group_id in perm else 0
        return base + rank * self._order_step

    def query(self, group_id, policy_version):
        """按规则组查询结论摘要。"""
        served = self._serve_versions.get(group_id, policy_version)
        groups = self._policies.get(served, {})
        delay = self._delay_for(group_id, groups)
        started = time.perf_counter_ns()
        if delay > 0:
            time.sleep(delay / 1000.0)
        summary = self._compute_summary(served, group_id)
        summary = self._maybe_corrupt(group_id, summary)
        finished = time.perf_counter_ns()
        with self._lock:
            self._log.append({
                "group_id": group_id,
                "served_version": served,
                "applied_delay_ms": delay,
                "started_ns": started,
                "finished_ns": finished,
            })
        return {"summary": summary, "served_version": served,
                "applied_delay_ms": delay}

    def query_log(self):
        with self._lock:
            return list(self._log)

    def _compute_summary(self, version, group_id):
        """服务端独立结论计算：端点原子段分解（与管线侧算法独立）。"""
        rules = [r for r in self._policies.get(version, {}).get(group_id, [])
                 if r["rule_id"] is not None]
        rules.sort(key=lambda r: r["rule_id"])

        points = sorted({r["lo"] for r in rules} | {r["hi"] for r in rules})
        atoms = [("pt", p) for p in points]
        atoms += [("seg", points[i], points[i + 1])
                  for i in range(len(points) - 1)]

        coverage = []
        for r in rules:
            covered = set()
            for idx, atom in enumerate(atoms):
                if atom[0] == "pt":
                    p = atom[1]
                    if ((r["lo"] < p < r["hi"])
                            or (p == r["lo"] and r["lc"])
                            or (p == r["hi"] and r["hc"])):
                        covered.add(idx)
                elif r["lo"] <= atom[1] and r["hi"] >= atom[2]:
                    covered.add(idx)
            coverage.append(covered)

        conflicts = []
        for i in range(len(rules)):
            for j in range(i + 1, len(rules)):
                if (coverage[i] & coverage[j]
                        and rules[i]["action"] != rules[j]["action"]):
                    conflicts.append([rules[i]["rule_id"], rules[j]["rule_id"]])

        shadowed = []
        for bi, b in enumerate(rules):
            for ai, a in enumerate(rules):
                if ai == bi:
                    continue
                if (a["priority"] is not None and b["priority"] is not None
                        and a["priority"] > b["priority"]
                        and coverage[bi] <= coverage[ai]):
                    shadowed.append({"rule": b["rule_id"], "by": a["rule_id"]})
        shadowed.sort(key=lambda rel: (rel["rule"], rel["by"]))

        return {
            "group_id": group_id,
            "policy_version": version,
            "summary_version": version,
            "conflicts": conflicts,
            "shadowed": shadowed,
        }

    def _maybe_corrupt(self, group_id, summary):
        mode = self._corrupt.get(group_id)
        if mode is None:
            return summary
        tampered = dict(summary)
        if mode == "drop_last_conflict":
            conflicts = [list(p) for p in summary["conflicts"]]
            if conflicts:
                conflicts.pop()
            else:
                conflicts.append(["zz_missing_a", "zz_missing_b"])
            tampered["conflicts"] = conflicts
        elif mode == "drop_last_shadowed":
            shadowed = [dict(rel) for rel in summary["shadowed"]]
            if shadowed:
                shadowed.pop()
            else:
                shadowed.append({"rule": "zz_missing", "by": "zz_rule"})
            tampered["shadowed"] = shadowed
        else:
            raise ValueError("未知篡改模式: %r" % (mode,))
        return tampered
