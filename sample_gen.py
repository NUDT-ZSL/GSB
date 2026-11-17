"""Deterministic local sample generator (requirement 8).

Generates policy declarations and task lists covering: clean, conflict,
shadow, dependency cycle, timeout, memory overflow, queue saturation,
concurrency equivalence, invalid entries and checkpoint recovery.

Everything is pure and deterministic - no randomness, no network, no clock.

Usage:
    python sample_gen.py --out samples/
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path


def _rule(rid, target, action, condition, priority, version="1.0.0",
          depends_on=None):
    rule = {
        "id": rid,
        "target": target,
        "action": action,
        "condition": condition,
        "priority": priority,
        "version": version,
    }
    if depends_on:
        rule["depends_on"] = depends_on
    return rule


def _tasks_from_rules(rules, simulate_by_task=None):
    groups = {}
    for rule in rules:
        groups.setdefault(rule["target"], []).append(rule["id"])
    tasks = []
    for target in sorted(groups):
        task = {
            "task_id": f"group:{target}",
            "target": target,
            "rule_ids": sorted(groups[target]),
        }
        if simulate_by_task and task["task_id"] in simulate_by_task:
            task["simulate"] = simulate_by_task[task["task_id"]]
        tasks.append(task)
    return {"tasks": tasks}


def scenario_clean():
    rules = [
        _rule("R-001", "web.frontend", "allow", 'proto == "tcp" and port == 443', 100),
        _rule("R-002", "web.frontend", "deny", 'proto == "udp" and port in [53, 123]', 200),
        _rule("R-003", "db.backend", "allow", 'src_zone == "internal" and port == 5432', 300),
        _rule("R-004", "db.backend", "audit", "risk_score >= 80", 400),
    ]
    return rules, _tasks_from_rules(rules)


def scenario_conflict():
    rules = [
        _rule("R-101", "api.gateway", "allow", "port >= 8000 and port <= 9000", 100),
        _rule("R-102", "api.gateway", "deny", "port >= 8500 and port <= 9500", 200),
        _rule("R-103", "api.gateway", "audit", 'proto == "tcp"', 300),
    ]
    return rules, _tasks_from_rules(rules)


def scenario_shadow():
    rules = [
        _rule("R-201", "mail.relay", "deny", "port >= 1 and port <= 65535", 900),
        _rule("R-202", "mail.relay", "allow", "port >= 20 and port <= 25", 100),
        _rule("R-203", "mail.relay", "allow", 'proto == "udp" and port == 53', 800),
    ]
    return rules, _tasks_from_rules(rules)


def scenario_cycle():
    rules = [
        _rule("R-301", "net.edge", "allow", "port == 8080", 100, depends_on=["R-302"]),
        _rule("R-302", "net.edge", "deny", "port == 8081", 200, depends_on=["R-303"]),
        _rule("R-303", "net.edge", "audit", "port == 8082", 300, depends_on=["R-301"]),
        _rule("R-304", "net.edge", "allow", "port == 9090", 400),
    ]
    return rules, _tasks_from_rules(rules)


def scenario_timeout():
    rules = [
        _rule("R-401", "slow.lane", "allow", "port == 1", 100),
        _rule("R-402", "slow.lane", "deny", "port == 2", 200),
        _rule("R-403", "fast.lane", "allow", "port == 3", 300),
    ]
    tasks = _tasks_from_rules(rules, simulate_by_task={
        "group:slow.lane": {"spin_ms": 800},
    })
    return rules, tasks


def scenario_memory():
    rules = [
        _rule("R-501", "big.job", "allow", "port == 10", 100),
        _rule("R-502", "small.job", "deny", "port == 20", 200),
    ]
    tasks = _tasks_from_rules(rules, simulate_by_task={
        "group:big.job": {"alloc_bytes": 32 * 1024 * 1024},
    })
    return rules, tasks


def scenario_queue_full():
    rules = []
    for idx in range(8):
        rules.append(_rule(f"R-6{idx:02d}", f"lane.{idx}", "allow",
                           f"port == {1000 + idx}", 100 + idx))
    tasks = _tasks_from_rules(rules, simulate_by_task={
        f"group:lane.{idx}": {"spin_ms": 250} for idx in range(8)
    })
    return rules, tasks


def scenario_concurrency():
    rules = []
    for idx in range(24):
        target = f"svc.{idx % 6}"
        action = ("allow", "deny", "audit")[idx % 3]
        lo = 100 * (idx % 4)
        rules.append(_rule(
            f"R-7{idx:02d}", target, action,
            f"port >= {lo} and port <= {lo + 150}", 100 + (idx % 5) * 100,
        ))
    return rules, _tasks_from_rules(rules)


def scenario_invalid():
    rules = [
        _rule("R-801", "ok.target", "allow", "port == 80", 100),
        {"id": "R-801", "target": "dup.target", "action": "deny",
         "condition": "port == 81", "priority": 10, "version": "1.0.0"},
        {"id": "R-802", "target": "BAD TARGET!", "action": "allow",
         "condition": "port == 82", "priority": 10, "version": "1.0.0"},
        {"id": "R-803", "target": "bad.cond", "action": "allow",
         "condition": "port === 83", "priority": 10, "version": "1.0.0"},
        {"id": "R-804", "target": "bad.op", "action": "allow",
         "condition": "port != 84", "priority": 10, "version": "1.0.0"},
        {"id": "R-805", "target": "bad.prio", "action": "allow",
         "condition": "port == 85", "priority": 99999, "version": "1.0.0"},
        {"id": "R-806", "target": "bad.ver", "action": "allow",
         "condition": "port == 86", "priority": 10, "version": "v1.2"},
        _rule("R-807", "ok.target", "deny", "port == 8080", 200),
    ]
    valid = [r for r in rules if r.get("id") in ("R-801", "R-807")]
    return rules, _tasks_from_rules(valid)


def scenario_recovery():
    rules = []
    for idx in range(6):
        rules.append(_rule(f"R-9{idx:02d}", f"zone.{idx}", "allow",
                           f"port == {2000 + idx}", 100 + idx))
    rules.append(_rule("R-910", "zone.0", "deny", "port == 2000", 500))
    return rules, _tasks_from_rules(rules)


SCENARIOS = {
    "clean": scenario_clean,
    "conflict": scenario_conflict,
    "shadow": scenario_shadow,
    "cycle": scenario_cycle,
    "timeout": scenario_timeout,
    "memory": scenario_memory,
    "queue_full": scenario_queue_full,
    "concurrency": scenario_concurrency,
    "invalid": scenario_invalid,
    "recovery": scenario_recovery,
}


def write_scenario(out_dir: Path, name: str) -> Path:
    rules, tasks = SCENARIOS[name]()
    scenario_dir = Path(out_dir) / name
    scenario_dir.mkdir(parents=True, exist_ok=True)
    policies = {"policy_version": "1.0.0", "rules": rules}
    (scenario_dir / "policies.json").write_text(
        json.dumps(policies, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    (scenario_dir / "tasks.json").write_text(
        json.dumps(tasks, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return scenario_dir


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--out", type=Path, default=Path("samples"))
    args = parser.parse_args()
    for name in SCENARIOS:
        path = write_scenario(args.out, name)
        print(f"wrote {path}")


if __name__ == "__main__":
    main()
