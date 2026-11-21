#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import sqlite3
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any

import numpy

ROOT = Path(__file__).resolve().parents[1]
SAMPLES = ROOT / "samples"


def run_cli(
    *args: str,
    env: dict[str, str] | None = None,
) -> tuple[int, dict[str, Any]]:
    process_env = os.environ.copy()
    process_env.pop("TIERLAB_CRASH_AT", None)
    if env:
        process_env.update(env)
    completed = subprocess.run(
        [sys.executable, "-m", "tierlab", *args],
        cwd=ROOT,
        env=process_env,
        text=True,
        capture_output=True,
        check=False,
    )
    try:
        payload = json.loads(completed.stdout)
    except json.JSONDecodeError:
        payload = {
            "stdout": completed.stdout,
            "stderr": completed.stderr,
        }
    return completed.returncode, payload


def ledger_matches(database: Path, bucket: str) -> bool:
    with sqlite3.connect(database) as connection:
        recorded = {
            row[0]: row[1]
            for row in connection.execute(
                "SELECT layer, used_bytes FROM layers WHERE bucket = ?",
                (bucket,),
            )
        }
        computed = {
            row[0]: row[1]
            for row in connection.execute(
                """
                SELECT current_layer, SUM(size_bytes)
                FROM objects WHERE bucket = ?
                GROUP BY current_layer
                """,
                (bucket,),
            )
        }
    return (
        recorded.get("hot", 0) == computed.get("hot", 0)
        and recorded.get("cold", 0) == computed.get("cold", 0)
    )


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--output",
        default="artifacts/validation_report.json",
    )
    args = parser.parse_args()
    output = Path(args.output)
    checks: list[dict[str, Any]] = []

    def record(name: str, ok: bool, details: dict[str, Any]) -> None:
        checks.append({"name": name, "ok": ok, "details": details})

    with tempfile.TemporaryDirectory(prefix="tierlab-demo-") as tmp:
        root = Path(tmp)

        exact_db = root / "exact.db"
        code, load = run_cli(
            "load",
            "--db",
            str(exact_db),
            "--manifest",
            str(SAMPLES / "small" / "manifest.json"),
            "--config",
            str(SAMPLES / "small" / "config.json"),
            "--clock",
            "1000",
        )
        code2, exact = run_cli(
            "migrate",
            "--db",
            str(exact_db),
            "--bucket",
            "small-a",
            "--round",
            "1",
            "--clock",
            "1100",
        )
        code3, verify_exact = run_cli("verify", "--db", str(exact_db))
        outcome_pairs = {
            (item["state"], item["outcome"]) for item in exact.get("outcomes", [])
        }
        record(
            "exact_small_round",
            code == code2 == code3 == 0
            and exact["mode"] == "exact"
            and verify_exact["ok"]
            and {
                ("committed", "started_success"),
                ("abandoned", "abandoned_budget"),
                ("abandoned", "abandoned_capacity"),
                ("not_started", "not_started_within_budget"),
            }
            <= outcome_pairs,
            {
                "load_ok": load.get("ok", False),
                "mode": exact["mode"],
                "candidate_count": exact["candidate_count"],
                "started_count": exact["started_count"],
                "outcomes": exact["outcomes"],
                "verify_ok": verify_exact["ok"],
                "ledger_before": exact["ledger_before"],
                "ledger_after": exact["ledger_after"],
            },
        )
        replay_code, replay = run_cli(
            "migrate",
            "--db",
            str(exact_db),
            "--bucket",
            "small-a",
            "--round",
            "1",
            "--clock",
            "1100",
        )
        record(
            "same_batch_idempotence",
            replay_code == 0 and replay == exact,
            {
                "returncode": replay_code,
                "bitwise_json_equal": replay == exact,
            },
        )
        status_before_rollback = run_cli(
            "status",
            "--db",
            str(exact_db),
        )[1]
        rollback_code, rollback = run_cli(
            "migrate",
            "--db",
            str(exact_db),
            "--bucket",
            "small-a",
            "--round",
            "2",
            "--clock",
            "900",
        )
        status_after_rollback = run_cli(
            "status",
            "--db",
            str(exact_db),
        )[1]
        record(
            "clock_rollback_rejected_without_view_change",
            rollback_code == 2
            and rollback.get("error", {}).get("code") == "E_CLOCK_ROLLBACK"
            and status_before_rollback == status_after_rollback,
            {
                "error": rollback.get("error"),
                "status_unchanged": status_before_rollback
                == status_after_rollback,
            },
        )

        sampled_db = root / "sampled.db"
        run_cli(
            "load",
            "--db",
            str(sampled_db),
            "--manifest",
            str(SAMPLES / "adaptive" / "manifest.json"),
            "--config",
            str(SAMPLES / "adaptive" / "config.json"),
            "--clock",
            "10000",
        )
        sampled_code, sampled = run_cli(
            "migrate",
            "--db",
            str(sampled_db),
            "--bucket",
            "adaptive-a",
            "--round",
            "1",
            "--clock",
            "10100",
        )
        verify_sampled = run_cli("verify", "--db", str(sampled_db))[1]
        sample_summary = sampled.get("observation", {}).get(
            "sampling_summary",
            {},
        )
        exact_adaptive_db = root / "adaptive-exact.db"
        run_cli(
            "load",
            "--db",
            str(exact_adaptive_db),
            "--manifest",
            str(SAMPLES / "adaptive" / "manifest.json"),
            "--config",
            str(SAMPLES / "adaptive" / "config.json"),
            "--clock",
            "10000",
        )
        exact_adaptive_code, exact_adaptive = run_cli(
            "migrate",
            "--db",
            str(exact_adaptive_db),
            "--bucket",
            "adaptive-a",
            "--round",
            "1",
            "--clock",
            "10100",
            "--mode",
            "exact",
        )
        count_interval = sample_summary.get("confidence_count_interval", [0, 0])
        bytes_interval = sample_summary.get("confidence_bytes_interval", [0, 0])
        record(
            "adaptive_sampled_round",
            sampled_code == 0
            and exact_adaptive_code == 0
            and sampled.get("mode") == "sampled"
            and verify_sampled.get("ok", False)
            and sample_summary.get("sampled_count", 1200) < 1200
            and count_interval[0]
            <= exact_adaptive.get("candidate_count", -1)
            <= count_interval[1]
            and bytes_interval[0]
            <= exact_adaptive.get("candidate_bytes", -1)
            <= bytes_interval[1],
            {
                "mode": sampled.get("mode"),
                "trigger_conditions": sampled.get("mode_decision", {}).get(
                    "trigger_conditions"
                ),
                "rejected_mode": sampled.get("mode_decision", {}).get(
                    "rejected_mode"
                ),
                "rejected_reason": sampled.get("mode_decision", {}).get(
                    "rejected_reason"
                ),
                "sampled_count": sample_summary.get("sampled_count"),
                "estimated_candidate_count": sample_summary.get(
                    "estimated_candidate_count"
                ),
                "candidate_count_error_bound": sample_summary.get(
                    "candidate_count_error_bound"
                ),
                "estimated_candidate_bytes": sample_summary.get(
                    "estimated_candidate_bytes"
                ),
                "candidate_bytes_error_bound": sample_summary.get(
                    "candidate_bytes_error_bound"
                ),
                "exact_candidate_count": exact_adaptive.get("candidate_count"),
                "exact_candidate_bytes": exact_adaptive.get("candidate_bytes"),
                "confidence_count_interval": count_interval,
                "confidence_bytes_interval": bytes_interval,
                "exact_truth_inside_intervals": (
                    count_interval[0]
                    <= exact_adaptive.get("candidate_count", -1)
                    <= count_interval[1]
                    and bytes_interval[0]
                    <= exact_adaptive.get("candidate_bytes", -1)
                    <= bytes_interval[1]
                ),
                "verify_ok": verify_sampled.get("ok", False),
            },
        )

        crash_db = root / "crash.db"
        run_cli(
            "load",
            "--db",
            str(crash_db),
            "--manifest",
            str(SAMPLES / "small" / "manifest.json"),
            "--config",
            str(SAMPLES / "small" / "config.json"),
            "--clock",
            "1000",
        )
        crash_code, _ = run_cli(
            "migrate",
            "--db",
            str(crash_db),
            "--bucket",
            "small-a",
            "--round",
            "1",
            "--clock",
            "1100",
            env={"TIERLAB_CRASH_AT": "after_reserve"},
        )
        crash_status = run_cli("status", "--db", str(crash_db))[1]["buckets"][0]
        pre_resume_verify_code, pre_resume_verify = run_cli(
            "verify",
            "--db",
            str(crash_db),
        )
        resume_code, resumed = run_cli("resume", "--db", str(crash_db))
        post_status = run_cli("status", "--db", str(crash_db))[1]["buckets"][0]
        post_verify = run_cli("verify", "--db", str(crash_db))[1]
        record(
            "forced_termination_and_resume",
            crash_code == 137
            and len(crash_status["inflight"]) == 2
            and pre_resume_verify_code == 3
            and "E_IN_FLIGHT_BATCH"
            in {item["code"] for item in pre_resume_verify["violations"]}
            and resume_code == 0
            and resumed["resumed_count"] == 1
            and post_status["inflight"] == []
            and post_verify["ok"]
            and ledger_matches(crash_db, "small-a"),
            {
                "crash_returncode": crash_code,
                "inflight_before_resume": len(crash_status["inflight"]),
                "pre_resume_verify_code": pre_resume_verify_code,
                "resumed_count": resumed.get("resumed_count"),
                "inflight_after_resume": len(post_status["inflight"]),
                "post_resume_verify_ok": post_verify["ok"],
                "ledger_matches_objects": ledger_matches(crash_db, "small-a"),
            },
        )

    report = {
        "report_schema": "tierlab.validation-report-v1",
        "environment": {
            "python": sys.version.split()[0],
            "numpy": numpy.__version__,
            "sqlite": sqlite3.sqlite_version,
        },
        "overall_ok": all(check["ok"] for check in checks),
        "checks": checks,
        "notes": [
            "All decisions use injected logical clocks; no wall clock is read.",
            "Temporary databases are removed after the deterministic report.",
        ],
    }
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(
        json.dumps(report, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
        encoding="utf-8",
    )
    print(json.dumps(report, ensure_ascii=False, sort_keys=True, indent=2))
    return 0 if report["overall_ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
