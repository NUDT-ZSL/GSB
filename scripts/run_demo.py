#!/usr/bin/env python3
"""Repeatable end-to-end demonstration on the deterministic sample."""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from elastic_control.engine import decide_round, load_workspace, resume
from elastic_control.samplegen import build_sample_payloads, generate_samples
from elastic_control.store import Store
from elastic_control.util import write_json_atomic


def run_checked(command: list[str], *, allowed: tuple[int, ...] = (0,)) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(
        command,
        cwd=ROOT,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    if result.returncode not in allowed:
        raise RuntimeError(
            f"command failed ({result.returncode}): {' '.join(command)}\n{result.stderr}"
        )
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--samples", default=str(ROOT / "samples"))
    parser.add_argument("--out", default=str(ROOT / "out"))
    args = parser.parse_args()
    samples_dir = Path(args.samples)
    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)
    generate_samples(samples_dir)
    rounds = build_sample_payloads()["samples.json"]["rounds"]

    main_db = out_dir / "control.db"
    load_workspace(data_dir=samples_dir, db_path=main_db, reset=True)
    main_store = Store(main_db)
    round_summaries = []
    for round_spec in rounds:
        result = decide_round(
            main_store,
            round_id=round_spec["round_id"],
            at_ms=round_spec["at_ms"],
            workloads=round_spec.get("workloads"),
            expected_version=1,
        )
        round_summaries.append(
            {
                "round_id": round_spec["round_id"],
                "at_ms": round_spec["at_ms"],
                "idempotent": bool(result["commit"].get("idempotent")),
                "actions": [
                    {
                        "workload": action["workload"],
                        "direction": action["direction"],
                        "delta": action["delta"],
                        "status": action["status"],
                        "reason": action["reason"],
                        "budget_charged": action["budget_charged"],
                    }
                    for action in result["status"]["actions"]
                    if action["round_id"] == round_spec["round_id"]
                ],
            }
        )
    main_status = main_store.status()
    main_verify = main_store.verify()
    write_json_atomic(out_dir / "demo_status.json", main_status)

    recovery_db = out_dir / "recovery_demo.db"
    load_workspace(data_dir=samples_dir, db_path=recovery_db, reset=True)
    base = [
        sys.executable,
        "-m",
        "elastic_control",
        "--db",
        str(recovery_db),
        "decide",
        "--round-id",
        "recovery-r1",
        "--at-ms",
        "2000",
        "--expected-version",
        "1",
    ]
    crashed = run_checked(base + ["--crash-after-start"], allowed=(97,))
    recovery_store = Store(recovery_db)
    before_resume = recovery_store.status()
    resumed = resume(recovery_store)
    after_resume = recovery_store.status()
    recovery_verify = recovery_store.verify()

    concurrent_db = out_dir / "concurrent_demo.db"
    load_workspace(data_dir=samples_dir, db_path=concurrent_db, reset=True)
    concurrent_base = [
        sys.executable,
        "-m",
        "elastic_control",
        "--db",
        str(concurrent_db),
        "decide",
        "--round-id",
        "r1",
        "--at-ms",
        "2000",
        "--expected-version",
        "1",
    ]
    first = subprocess.Popen(
        concurrent_base + ["--workload", "api-web"],
        cwd=ROOT,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        text=True,
    )
    second = subprocess.Popen(
        concurrent_base
        + [
            "--workload",
            "worker",
            "--workload",
            "batch",
            "--workload",
            "archive",
        ],
        cwd=ROOT,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        text=True,
    )
    _, first_err = first.communicate()
    _, second_err = second.communicate()
    if first.returncode != 0 or second.returncode != 0:
        raise RuntimeError(
            f"concurrent decide failed: {first.returncode}/{second.returncode}\n"
            f"{first_err}\n{second_err}"
        )
    concurrent_store = Store(concurrent_db)
    concurrent_status = concurrent_store.status()
    concurrent_verify = concurrent_store.verify()

    report = {
        "ok": bool(
            main_verify["ok"]
            and recovery_verify["ok"]
            and concurrent_verify["ok"]
        ),
        "main": {
            "db": str(main_db.relative_to(ROOT)) if main_db.is_relative_to(ROOT) else str(main_db),
            "rounds": round_summaries,
            "budget_used_cpu_core_ms": main_status["budget"]["used_cpu_core_ms"],
            "replicas": {
                name: item["replicas"]
                for name, item in main_status["workloads"].items()
            },
            "precision_flags": {
                name: [flag["type"] for flag in item["precision_flags"]]
                for name, item in main_status["workloads"].items()
                if item["precision_flags"]
            },
            "verify": main_verify,
        },
        "forced_termination_recovery": {
            "crash_exit_code": crashed.returncode,
            "budget_before_resume": before_resume["budget"]["used_cpu_core_ms"],
            "budget_after_resume": after_resume["budget"]["used_cpu_core_ms"],
            "resumed_rounds": resumed["resumed_rounds"],
            "replicas": {
                name: item["replicas"]
                for name, item in after_resume["workloads"].items()
            },
            "verify": recovery_verify,
        },
        "concurrent_instances": {
            "budget_used_cpu_core_ms": concurrent_status["budget"]["used_cpu_core_ms"],
            "pool_cpu_core_ms": concurrent_status["budget"]["limits"]["pool_cpu_core_ms"],
            "actions": [
                {
                    "workload": action["workload"],
                    "status": action["status"],
                    "budget_charged": action["budget_charged"],
                }
                for action in concurrent_status["actions"]
            ],
            "verify": concurrent_verify,
        },
    }
    write_json_atomic(out_dir / "demo_report.json", report)
    print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True))
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
