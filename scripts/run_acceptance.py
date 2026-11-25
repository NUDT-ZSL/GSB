#!/usr/bin/env python3
"""Run the task acceptance path and write a machine-readable report."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from drift_ae.demo import run_demo
from drift_ae.fixtures import build_fixture_bundle
from drift_ae.replay import replay_file


def _load_result(state: Path) -> dict:
    return json.loads((state / "snapshots" / "result" / "current.json").read_text(encoding="utf-8"))["payload"]


def _run_example(args: list[str]) -> subprocess.CompletedProcess:
    env = dict(os.environ)
    env["PYTHONPATH"] = str(ROOT) + os.pathsep + env.get("PYTHONPATH", "")
    return subprocess.run(
        [sys.executable, str(ROOT / "examples" / "end_to_end_demo.py"), *args],
        cwd=ROOT,
        env=env,
        text=True,
        capture_output=True,
        timeout=120,
    )


def main() -> int:
    acceptance_dir = ROOT / "artifacts" / "acceptance"
    shutil.rmtree(acceptance_dir, ignore_errors=True)
    fixtures_dir = acceptance_dir / "fixtures"
    baseline_state = acceptance_dir / "baseline"
    recovery_state = acceptance_dir / "recovery"
    acceptance_dir.mkdir(parents=True, exist_ok=True)

    fixture = build_fixture_bundle(fixtures_dir)

    tests = subprocess.run(
        [sys.executable, "-m", "unittest", "discover", "-s", "tests"],
        cwd=ROOT,
        text=True,
        capture_output=True,
        timeout=180,
        env={**os.environ, "PYTHONPATH": str(ROOT)},
    )
    (acceptance_dir / "unittest.log").write_text(tests.stdout + "\n" + tests.stderr, encoding="utf-8")

    baseline_summary = run_demo(
        state_dir=baseline_state,
        fixtures_dir=fixtures_dir,
        fresh=True,
        batch_size=8,
    )

    crash = _run_example(
        [
            "--fixtures-dir",
            str(fixtures_dir),
            "--state-dir",
            str(recovery_state),
            "--fresh",
            "--batch-size",
            "8",
            "--crash-after",
            "3",
        ]
    )
    (acceptance_dir / "crash.stdout.log").write_text(crash.stdout, encoding="utf-8")
    (acceptance_dir / "crash.stderr.log").write_text(crash.stderr, encoding="utf-8")

    resume = _run_example(
        [
            "--fixtures-dir",
            str(fixtures_dir),
            "--state-dir",
            str(recovery_state),
            "--batch-size",
            "8",
        ]
    )
    (acceptance_dir / "resume.stdout.log").write_text(resume.stdout, encoding="utf-8")
    (acceptance_dir / "resume.stderr.log").write_text(resume.stderr, encoding="utf-8")

    baseline_result = _load_result(baseline_state)
    recovered_result = _load_result(recovery_state)
    baseline_checksums = [item["record_checksum"] for item in baseline_result["records"]]
    recovered_checksums = [item["record_checksum"] for item in recovered_result["records"]]
    replay = replay_file(recovery_state / "diagnostics.jsonl")
    recovered_summary = json.loads((recovery_state / "demo_summary.json").read_text(encoding="utf-8"))

    checks = {
        "unit_tests_pass": tests.returncode == 0,
        "sigkill_observed": crash.returncode == -9,
        "resume_pass": resume.returncode == 0,
        "committed_batches_reused": recovered_summary["run"]["reused_count"] == 24,
        "remaining_batches_processed": recovered_summary["run"]["processed_count"] == 72,
        "decisions_match_baseline": recovered_result["decisions"] == baseline_result["decisions"],
        "record_checksums_match_baseline": recovered_checksums == baseline_checksums,
        "replay_pass": replay["ok"],
        "fixture_seed": fixture["manifest"]["seed"],
        "window_count": fixture["manifest"]["window_count"],
    }
    report = {
        "ok": all(value is True for key, value in checks.items() if key.endswith("pass") or key in {
            "sigkill_observed",
            "committed_batches_reused",
            "remaining_batches_processed",
            "decisions_match_baseline",
            "record_checksums_match_baseline",
        }),
        "checks": checks,
        "unittest_returncode": tests.returncode,
        "crash_returncode": crash.returncode,
        "resume_returncode": resume.returncode,
        "replay_record_count": replay["record_count"],
        "baseline_record_count": len(baseline_checksums),
        "recovered_record_count": len(recovered_checksums),
        "snapshot_versions": recovered_summary["snapshot_versions"],
    }
    (acceptance_dir / "acceptance_report.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
