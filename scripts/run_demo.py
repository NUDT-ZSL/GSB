#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from lease_arbiter.samplegen import generate_samples


def run_cli(base: list[str], *args: str, env: dict[str, str] | None = None) -> dict[str, Any]:
    command = [sys.executable, "-m", "lease_arbiter", *base, *args]
    completed = subprocess.run(
        command,
        cwd=ROOT,
        text=True,
        capture_output=True,
        env={**os.environ, **(env or {})},
    )
    try:
        payload = json.loads(completed.stdout)
    except json.JSONDecodeError as exc:
        raise RuntimeError(
            f"command did not return JSON: {' '.join(command)}\n"
            f"stdout={completed.stdout}\nstderr={completed.stderr}"
        ) from exc
    if completed.returncode != 0 or not payload.get("ok", False):
        raise RuntimeError(
            f"command failed ({completed.returncode}): {' '.join(command)}\n"
            f"{json.dumps(payload, ensure_ascii=False, indent=2)}\n{completed.stderr}"
        )
    return payload


def file_hashes(directory: Path) -> dict[str, str]:
    return {
        path.name: hashlib.sha256(path.read_bytes()).hexdigest()
        for path in sorted(directory.glob("*.json"))
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Run the complete deterministic acceptance demo")
    parser.add_argument("--data", default="data")
    parser.add_argument("--db", default="var/ledger.db")
    parser.add_argument("--pointer", default="var/ledger.pointer.json")
    parser.add_argument("--reports", default="reports")
    parser.add_argument(
        "--result", default="artifacts/demo_result.json", help="compact result file"
    )
    args = parser.parse_args()

    data_dir = ROOT / args.data
    generate_samples(data_dir)
    hashes_before = file_hashes(data_dir)
    base = ["--db", args.db, "--pointer", args.pointer, "--reports", args.reports]
    load_args = [
        "--cards",
        str(data_dir / "accelerator_cards.json"),
        "--jobs",
        str(data_dir / "jobs.json"),
        "--config",
        str(data_dir / "lease_config.json"),
        "--anomalies",
        str(data_dir / "anomaly_injections.json"),
        "--force",
    ]
    load = run_cli(base, "load", *load_args)
    rounds: dict[str, Any] = {}
    for round_no in (1, 2, 3):
        rounds[str(round_no)] = run_cli(base, "lease", "--round", str(round_no))

    preempt = run_cli(
        base, "preempt", "--round", "3", "--job-id", "job-900", "--apply"
    )
    job_100_lease = next(
        grant["lease_id"]
        for grant in rounds["1"]["grants"]
        if grant["job_id"] == "job-100"
    )
    rounds["4"] = run_cli(
        base,
        "lease",
        "--round",
        "4",
        "--renew",
        f"{job_100_lease}:job-100:1",
    )
    for round_no in (5, 6, 7, 8, 9):
        rounds[str(round_no)] = run_cli(base, "lease", "--round", str(round_no))

    job_900_lease = preempt["grants"][0]["lease_id"]
    rounds["10"] = run_cli(
        base,
        "lease",
        "--round",
        "10",
        "--renew",
        f"{job_900_lease}:job-900:1",
    )
    verify = run_cli(base, "verify")
    status = run_cli(base, "status")

    assert rounds["3"]["denials"][0]["job_id"] == "job-900"
    assert rounds["3"]["denials"][0]["first_constraint"]["constraint"] == (
        "quota_group_compute_limit"
    )
    assert [item["job_id"] for item in preempt["selection_chain"]] == ["job-700"]
    assert rounds["8"]["denials"][0]["job_id"] == "job-1100"
    assert rounds["8"]["denials"][0]["fragment_classification"] == "fragmentation"
    codes = {item["code"] for item in rounds["10"]["anomalies"]}
    assert codes == {
        "ARB-ANOM-001",
        "ARB-ANOM-002",
        "ARB-ANOM-003",
        "ARB-ANOM-004",
    }, codes
    assert {grant["card_id"] for grant in rounds["10"]["grants"]} == {"card-b"}
    assert verify["ok"] is True
    assert file_hashes(data_dir) == hashes_before

    result = {
        "ok": True,
        "load_version": load["version"],
        "rounds": {
            round_no: {
                "ledger_version": payload.get("ledger_version"),
                "grants": [
                    {
                        "job_id": grant["job_id"],
                        "card_id": grant["card_id"],
                        "lease_id": grant["lease_id"],
                        "compute": [grant["compute_start"], grant["compute_length"]],
                        "memory": [grant["memory_start"], grant["memory_length"]],
                    }
                    for grant in payload.get("grants", [])
                ],
                "expirations": [
                    {"job_id": item["job_id"], "outcome": item["outcome"]}
                    for item in payload.get("expirations", [])
                ],
                "denials": [
                    {
                        "job_id": item["job_id"],
                        "constraint": item["first_constraint"]["constraint"],
                        "fragment_classification": item["fragment_classification"],
                    }
                    for item in payload.get("denials", [])
                ],
                "anomalies": [
                    {"code": item["code"], "card_id": item["card_id"]}
                    for item in payload.get("anomalies", [])
                ],
            }
            for round_no, payload in rounds.items()
        },
        "preemption": {
            "chain": [item["job_id"] for item in preempt["selection_chain"]],
            "refunds": [
                {
                    "lease_id": item["lease_id"],
                    "refund_compute": item["refund_compute"],
                    "status": item["status"],
                }
                for item in preempt["refund_records"]
            ],
            "grant_job": preempt["grants"][0]["job_id"],
        },
        "verify": {
            "ok": verify["ok"],
            "ledger_version": verify["ledger_version"],
            "checks": len(verify["checks"]),
            "failure_count": verify["failure_count"],
        },
        "status": {
            "ledger_version": status["ledger_version"],
            "pointer_consistent": status["pointer_consistent"],
            "active_leases": len(status["active_leases"]),
            "waiting_jobs": [item["job_id"] for item in status["waiting_jobs"]],
            "pending_refunds": len(status["pending_refunds"]),
            "anomalies": [item["code"] for item in status["anomalies"]],
        },
        "sample_hashes_unchanged": True,
        "sample_hashes": hashes_before,
    }
    result_path = ROOT / args.result
    result_path.parent.mkdir(parents=True, exist_ok=True)
    result_path.write_text(
        json.dumps(result, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
        encoding="utf-8",
    )
    print(json.dumps(result, ensure_ascii=False, sort_keys=True, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
