from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SAMPLES = ROOT / "samples"
ARTIFACTS = ROOT / "artifacts"


def run(arguments: list[str], env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
    environment = {
        key: value
        for key, value in os.environ.items()
        if key != "TAILSAMPLE_FAULT_POINT"
    }
    environment["PYTHONHASHSEED"] = "0"
    environment.update(env or {})
    return subprocess.run(
        [sys.executable, "-m", "tailsample", *arguments],
        cwd=ROOT,
        capture_output=True,
        text=True,
        encoding="utf-8",
        env=environment,
        check=False,
    )


def load(db: Path) -> None:
    completed = run(
        [
            "load",
            "--db",
            str(db),
            "--topology",
            str(SAMPLES / "topology.json"),
            "--policies",
            str(SAMPLES / "policies.json"),
            "--events",
            str(SAMPLES / "events.json"),
            "--window-ms",
            "900",
            "--quota-per-second",
            "2",
            "--clock-tolerance-ms",
            "10",
            "--reset",
        ]
    )
    if completed.returncode != 0:
        raise RuntimeError(completed.stderr)


def query_views(db: Path) -> dict[str, object]:
    result: dict[str, object] = {}
    for trace_id in (
        "trace-checkout-001",
        "trace-slow-003",
        "trace-policy-006",
        "trace-fast-000",
    ):
        completed = run(["query", "--db", str(db), "--trace-id", trace_id])
        if completed.returncode != 0:
            raise RuntimeError(completed.stderr)
        result[trace_id] = json.loads(completed.stdout)
    return result


def main() -> int:
    ARTIFACTS.mkdir(parents=True, exist_ok=True)
    baseline_db = ARTIFACTS / "recovery_baseline.db"
    crash_db = ARTIFACTS / "recovery_crash.db"

    load(baseline_db)
    baseline = run(
        ["sample", "--db", str(baseline_db), "--advance-to", "4000"]
    )
    if baseline.returncode != 0:
        raise RuntimeError(baseline.stderr)

    load(crash_db)
    crashed = run(
        ["sample", "--db", str(crash_db), "--advance-to", "4000"],
        env={"TAILSAMPLE_FAULT_POINT": "after_quota"},
    )
    after_crash = run(["status", "--db", str(crash_db)])
    if after_crash.returncode != 0:
        raise RuntimeError(after_crash.stderr)
    resumed = run(["resume", "--db", str(crash_db)])
    if resumed.returncode != 0:
        raise RuntimeError(resumed.stderr)

    baseline_views = query_views(baseline_db)
    recovered_views = query_views(crash_db)
    baseline_verify = json.loads(
        run(["verify", "--db", str(baseline_db)]).stdout
    )
    recovered_verify = json.loads(
        run(["verify", "--db", str(crash_db)]).stdout
    )
    crash_status = json.loads(after_crash.stdout)
    resume_result = json.loads(resumed.stdout)
    summary = {
        "fault_exit_code": crashed.returncode,
        "state_after_fault": {
            "decisions": crash_status["decisions"]["total"],
            "quota_used": crash_status["quota"]["used"],
        },
        "resume": {
            "events_replayed": resume_result["events_replayed"],
            "decisions_replayed": resume_result["decisions_replayed"],
            "recovery_runs": resume_result["recovery_runs"],
        },
        "trace_views_identical": baseline_views == recovered_views,
        "baseline_verify": baseline_verify["ok"],
        "recovered_verify": recovered_verify["ok"],
        "baseline_decisions": baseline_verify["summary"]["decisions"],
        "recovered_decisions": recovered_verify["summary"]["decisions"],
    }
    print(json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True))
    return (
        0
        if crashed.returncode == 97
        and summary["trace_views_identical"]
        and baseline_verify["ok"]
        and recovered_verify["ok"]
        else 1
    )


if __name__ == "__main__":
    raise SystemExit(main())

