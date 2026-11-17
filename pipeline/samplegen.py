"""确定性本地样本生成器（业务要求 8）。

生成 policy.json / policy_v0.json / requests.json / service_config.json
与 truth.json（每条请求的服务侧同步标记与期望结论摘要真值）。
验证端只依据 truth.json 中的标记与管线输出比较。
"""

from __future__ import annotations

import json
import os

from . import localcalc, normalize
from .service import LocalPolicyService


def _rule(rid, lower, upper, action, priority,
          lower_endpoint="inclusive", upper_endpoint="inclusive"):
    return {
        "rule_id": rid,
        "condition": {
            "lower": lower,
            "upper": upper,
            "lower_endpoint": lower_endpoint,
            "upper_endpoint": upper_endpoint,
        },
        "action": action,
        "priority": priority,
    }


def build_policy_v1():
    return {
        "policy_version": "v1",
        "rule_groups": [
            {"group_id": "g1", "rules": [
                _rule("r1", 0, 10, "allow", 1),
                _rule("r2", 20, 30, "deny", 2),
            ]},
            {"group_id": "g2", "rules": [
                _rule("r1", 0, 10, "allow", 1),
                _rule("r2", 5, 15, "deny", 2),
            ]},
            {"group_id": "g3", "rules": [
                _rule("r1", 0, 100, "allow", 5),
                _rule("r2", 10, 20, "allow", 1),
            ]},
            {"group_id": "g4", "rules": [
                _rule("r1", 0, 10, "allow", 1),
                _rule("r2", 5, 15, "deny", 2),
                _rule("r3", 40, 50, "allow", 3),
            ]},
            {"group_id": "g5", "rules": [
                _rule("r1", 0, 10, "allow", 1),
                _rule("r2", 8, 20, "deny", 2),
            ]},
            {"group_id": "g6", "rules": [
                _rule("r1", 0, 5, "allow", 1),
            ]},
            {"group_id": "g7", "rules": [
                _rule("r1", 0, 5, "allow", 1),
                _rule("r2", 5, 9, "deny", 2, lower_endpoint="exclusive"),
            ]},
            {"group_id": "g8", "rules": [
                _rule("r1", 0, 100, "deny", 9),
                _rule("r2", 10, 20, "allow", 1),
                _rule("r3", 30, 40, "allow", 2),
            ]},
        ],
    }


def build_policy_v0():
    """历史版本：g5 的区间不同，结论摘要与 v1 不同。"""
    decl = build_policy_v1()
    decl["policy_version"] = "v0"
    for group in decl["rule_groups"]:
        if group["group_id"] == "g5":
            group["rules"] = [
                _rule("r1", 0, 10, "allow", 1),
                _rule("r2", 30, 40, "deny", 2),
            ]
    return decl


SERVICE_CONFIG = {
    "delay_tiers_ms": {
        "g1": 5, "g2": 40, "g3": 10, "g4": 20,
        "g5": 15, "g6": 300, "g7": 5, "g8": 8,
    },
    "order_mode": "reverse",
    "order_step_ms": 5,
    "serve_versions": {"g5": "v0"},
    "corrupt": {"g4": "drop_last_conflict"},
    "summary_version": "svc-1",
    "seed": 27,
}

# 各请求期望返回时限（毫秒）。g6 的档位延迟 300ms 远超 100ms 时限 → 超时；
# 其余请求时限留出充足余量，保证任意并发度/乱序模式下判定稳定。
DEADLINES_MS = {
    "g1": 2000, "g2": 2000, "g3": 2000, "g4": 2000,
    "g5": 2000, "g6": 100, "g7": 2000, "g8": 2000,
}


def make_service(config=None):
    cfg = dict(SERVICE_CONFIG if config is None else config)
    service = LocalPolicyService(
        delay_tiers_ms=cfg.get("delay_tiers_ms"),
        order_mode=cfg.get("order_mode", "normal"),
        order_step_ms=cfg.get("order_step_ms", 5),
        serve_versions=cfg.get("serve_versions"),
        corrupt=cfg.get("corrupt"),
        summary_version=cfg.get("summary_version", "svc-1"),
        seed=cfg.get("seed", 27),
    )
    service.load_declaration(build_policy_v1())
    service.load_declaration(build_policy_v0())
    return service


def build_requests(decl):
    """依据管线侧独立计算生成期望结论摘要（作为请求清单输入）。"""
    groups, version, rejects = normalize.normalize_policy(decl)
    assert not rejects, rejects
    requests = []
    for index, gid in enumerate(sorted(groups.keys()), start=1):
        summary = localcalc.compute_summary(groups[gid], version)
        requests.append({
            "request_id": "q%d" % index,
            "group_id": gid,
            "deadline_ms": DEADLINES_MS[gid],
            "expected_summary": {
                "conflicts": summary["conflicts"],
                "shadowed": summary["shadowed"],
            },
        })
    return {"requests": requests}


def build_truth(decl, manifest, service_config=None):
    """真值文件：服务侧同步标记 + 期望结论摘要 + 期望状态。"""
    service = make_service(service_config)
    schedule = service.delay_schedule(decl["policy_version"])
    groups, version, _ = normalize.normalize_policy(decl)
    cfg = SERVICE_CONFIG if service_config is None else service_config
    tiers = cfg.get("delay_tiers_ms", {})

    entries = []
    for req in manifest["requests"]:
        gid = req["group_id"]
        total = schedule[gid]
        base = tiers.get(gid, 0)
        if gid in cfg.get("corrupt", {}):
            expected_status = "E_MISMATCH"
        elif gid in cfg.get("serve_versions", {}):
            expected_status = "E_STALE"
        else:
            expected_status = "OK"
        entries.append({
            "request_id": req["request_id"],
            "group_id": gid,
            "expected_status": expected_status,
            "expect_timed_out": total > req["deadline_ms"],
            "sync_marker": {
                "delay_tier_ms": base,
                "order_adjust_ms": total - base,
                "total_delay_ms": total,
            },
            "expected_summary": localcalc.compute_summary(groups[gid], version),
        })
    return {"policy_version": version, "requests": entries}


def _write(path, obj):
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(obj, fh, ensure_ascii=False, indent=2, sort_keys=True)


def generate_samples(out_dir):
    os.makedirs(out_dir, exist_ok=True)
    decl = build_policy_v1()
    manifest = build_requests(decl)
    _write(os.path.join(out_dir, "policy.json"), decl)
    _write(os.path.join(out_dir, "policy_v0.json"), build_policy_v0())
    _write(os.path.join(out_dir, "requests.json"), manifest)
    _write(os.path.join(out_dir, "service_config.json"), SERVICE_CONFIG)
    _write(os.path.join(out_dir, "truth.json"), build_truth(decl, manifest))
    return out_dir
