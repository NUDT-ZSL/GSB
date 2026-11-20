#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from leasecoord.config import parse_clock_steps, read_json  # noqa: E402
from leasecoord.db import answer_json  # noqa: E402
from leasecoord.engine import Engine  # noqa: E402
from scripts.concurrency_check import run_check as run_concurrency  # noqa: E402
from scripts.crash_recovery_check import run_check as run_crash_recovery  # noqa: E402


def _clean_runtime(workspace: Path) -> None:
    workspace.mkdir(parents=True, exist_ok=True)
    for name in ("coord.db", "coord.db-wal", "coord.db-shm", ".coord.lock"):
        (workspace / name).unlink(missing_ok=True)


def run_demo(workspace: Path, *, workers: int) -> dict[str, Any]:
    sample = ROOT / "workspace"
    workspace = workspace.resolve()
    _clean_runtime(workspace)
    nodes = read_json(sample / "nodes.json")
    election = read_json(sample / "election_config.json")
    steps = parse_clock_steps(read_json(sample / "clock_steps.json"))

    engine = Engine(workspace)
    loaded = engine.load(nodes, election, clock="0", reset=True)
    initial: dict[str, Any] | None = None
    failover: dict[str, Any] | None = None
    status_view: dict[str, Any] | None = None
    verification: dict[str, Any] | None = None
    renewals = 0
    executed: list[dict[str, Any]] = []

    for step in steps:
        op = step["op"]
        clock = step["clock_ms"]
        if op == "elect":
            result = engine.elect(clock=clock)
            if clock == "0":
                initial = result
            if clock == "1000":
                failover = result
            executed.append(
                {
                    "op": op,
                    "clock_ms": clock,
                    "leader": result["leader"],
                    "precision_sensitive": result["precision_sensitive"],
                }
            )
        elif op == "renew":
            result = engine.renew(
                node=step["node"],
                session_epoch=step["session_epoch"],
                clock=clock,
            )
            renewals += 1
            executed.append(
                {
                    "op": op,
                    "clock_ms": clock,
                    "node": step["node"],
                    "leader": result["leader"],
                }
            )
        elif op == "verify":
            verification = engine.verify()
            executed.append(
                {"op": op, "clock_ms": clock, "verified": verification["verified"]}
            )
        elif op == "status":
            status_view = engine.status(clock=clock)
            executed.append(
                {
                    "op": op,
                    "clock_ms": clock,
                    "leader": status_view["leader"],
                    "boundary_candidates": status_view["boundary_candidates"],
                }
            )
        elif op == "resume":
            result = engine.resume(clock=clock)
            executed.append(
                {
                    "op": op,
                    "clock_ms": clock,
                    "leader": result["leader"],
                }
            )

    status_a = engine.status(clock="1000")
    status_b = engine.status(clock="1000")
    repeat_identical = answer_json(status_a) == answer_json(status_b)
    status_final = status_view or status_a
    verification = verification or engine.verify()
    concurrency = run_concurrency(workspace / "runtime_concurrency", workers=workers)
    crash_recovery = run_crash_recovery(workspace / "runtime_crash")
    database = engine.inspect_database()
    pair = failover["precision_pairs"][0] if failover and failover["precision_pairs"] else None
    ok = bool(
        initial
        and initial["leader"] == "bravo"
        and failover
        and failover["leader"] == "charlie"
        and failover["precision_sensitive"]
        and pair
        and pair["gap_ms"] == "0.0002"
        and status_final["boundary_candidates"] == ["alpha", "charlie"]
        and repeat_identical
        and verification["verified"]
        and concurrency["ok"]
        and crash_recovery["ok"]
    )
    return {
        "ok": ok,
        "engine_version": "1.0.0",
        "python": sys.version.split()[0],
        "sample": {
            "config_hash": loaded["config_hash"],
            "nodes": [node["name"] for node in loaded["nodes"]],
            "steps_executed": len(executed),
            "renewals_executed": renewals,
            "initial_election": {
                "leader": initial["leader"] if initial else None,
                "bootstrapped_nodes": initial["bootstrapped_nodes"] if initial else [],
            },
            "failover_election": {
                "clock_ms": failover["election_clock_ms"] if failover else None,
                "leader": failover["leader"] if failover else None,
                "first_rejection": failover["first_rejection"] if failover else None,
                "precision_sensitive": (
                    failover["precision_sensitive"] if failover else None
                ),
                "precision_pair": pair,
            },
            "boundary_view": {
                "clock_ms": status_final["view_clock_ms"],
                "watermark_ms": status_final["watermark_ms"],
                "boundary_candidates": status_final["boundary_candidates"],
                "margin_to_safety_ms": {
                    node["name"]: node["lease"]["margin_to_safety_ms"]
                    for node in status_final["nodes"]
                },
            },
            "status_repeat_identical": repeat_identical,
            "verification": {
                "verified": verification["verified"],
                "check_names": [check["name"] for check in verification["checks"]],
            },
            "database": database,
        },
        "concurrency": concurrency,
        "crash_recovery": crash_recovery,
        "interactive_commands": [
            "python -m leasecoord load --workspace workspace --reset --clock 0",
            "python -m leasecoord elect --workspace workspace --clock 0",
            "python -m leasecoord renew --workspace workspace --node alpha --session-epoch 1 --clock 100",
            "python -m leasecoord status --workspace workspace --clock 1000",
            "python -m leasecoord verify --workspace workspace",
        ],
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--workspace", default="workspace")
    parser.add_argument("--workers", type=int, default=8)
    parser.add_argument("--compact", action="store_true")
    args = parser.parse_args()
    result = run_demo(Path(args.workspace), workers=args.workers)
    print(answer_json(result, compact=args.compact))
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
