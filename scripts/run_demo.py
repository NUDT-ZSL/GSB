#!/usr/bin/env python3
from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
SAMPLES = ROOT / "samples"
ARTIFACTS = ROOT / "artifacts"


def sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def run_cli(
    python: str,
    args: list[str],
    *,
    env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    merged = os.environ.copy()
    merged.pop("FLOWAGG_FAULT_POINT", None)
    if env:
        merged.update(env)
    return subprocess.run(
        [python, "-m", "flowagg", *args],
        cwd=ROOT,
        env=merged,
        text=True,
        capture_output=True,
        check=False,
    )


def require_ok(result: subprocess.CompletedProcess[str], label: str) -> None:
    if result.returncode != 0:
        raise RuntimeError(
            f"{label} failed with exit {result.returncode}\n"
            f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
        )


def prepare_run(run_dir: Path) -> Path:
    if run_dir.exists():
        shutil.rmtree(run_dir)
    (run_dir / "spill").mkdir(parents=True)
    plan = json.loads((SAMPLES / "plan.json").read_text(encoding="utf-8"))
    plan["spill_dir"] = "spill"
    plan_path = run_dir / "plan.json"
    plan_path.write_text(
        json.dumps(plan, sort_keys=True, indent=2) + "\n",
        encoding="utf-8",
    )
    return plan_path


def normal_run(python: str, run_dir: Path) -> dict[str, Any]:
    plan_path = prepare_run(run_dir)
    db_path = run_dir / "state.db"
    common = ["--plan", str(plan_path), "--db", str(db_path)]
    outputs: dict[str, Path] = {
        "load": run_dir / "load.json",
        "aggregate": run_dir / "aggregate.json",
        "status": run_dir / "status.json",
        "verify": run_dir / "verify.json",
    }
    calls = [
        ("load", ["load", *common, "--output", str(outputs["load"])]),
        (
            "aggregate",
            [
                "aggregate",
                *common,
                "--manifest",
                str(SAMPLES / "manifest.json"),
                "--output",
                str(outputs["aggregate"]),
            ],
        ),
        (
            "status",
            ["status", *common, "--with-groups", "--output", str(outputs["status"])],
        ),
        (
            "verify",
            [
                "verify",
                *common,
                "--manifest",
                str(SAMPLES / "manifest.json"),
                "--output",
                str(outputs["verify"]),
            ],
        ),
    ]
    exit_codes: dict[str, int] = {}
    for label, args in calls:
        result = run_cli(python, args)
        require_ok(result, f"{run_dir.name}/{label}")
        exit_codes[label] = result.returncode
    verification = json.loads(outputs["verify"].read_text(encoding="utf-8"))
    summary = json.loads(outputs["aggregate"].read_text(encoding="utf-8"))
    clock_steps = json.loads(
        (SAMPLES / "clock_steps.json").read_text(encoding="utf-8")
    )["steps"]
    batch_results = summary["batches"]
    if len(batch_results) != len(clock_steps):
        raise RuntimeError("clock step table and aggregate batch count differ")
    for step, batch_result in zip(clock_steps, batch_results, strict=True):
        if batch_result["batch"]["batch_id"] != step["batch_id"]:
            raise RuntimeError("clock step batch ordering differs")
        if (
            batch_result["batch"]["logical_watermark_ms"]
            != step["expected_watermark_ms"]
        ):
            raise RuntimeError(
                f"watermark mismatch for {step['batch_id']}: "
                f"{batch_result['batch']['logical_watermark_ms']} != "
                f"{step['expected_watermark_ms']}"
            )
    if verification["status"]["logical_watermark_ms"] != clock_steps[-1][
        "expected_watermark_ms"
    ]:
        raise RuntimeError("final logical watermark differs from clock step table")
    status = verification["status"]
    return {
        "exit_codes": exit_codes,
        "groups_verified": verification["groups_verified"],
        "records_verified": verification["records_verified"],
        "spill_files": status["spill"]["files"],
        "spill_groups": status["spill"]["groups"],
        "spill_bytes": status["spill"]["bytes"],
        "readbacks": status["readback"]["readbacks"],
        "readback_files": status["readback"]["readback_files"],
        "readback_bytes": status["readback"]["readback_bytes"],
        "state_hash": verification["state_hash"],
        "aggregate_ok": summary["ok"],
        "clock_steps_verified": len(clock_steps),
        "output_sha256": {
            name: sha256_file(path) for name, path in sorted(outputs.items())
        },
    }


def crash_run(python: str, run_dir: Path) -> dict[str, Any]:
    plan_path = prepare_run(run_dir)
    db_path = run_dir / "state.db"
    common = ["--plan", str(plan_path), "--db", str(db_path)]
    crash = run_cli(
        python,
        [
            "aggregate",
            *common,
            "--manifest",
            str(SAMPLES / "manifest.json"),
        ],
        env={"FLOWAGG_FAULT_POINT": "after_spill"},
    )
    if crash.returncode == 0:
        raise RuntimeError("fault-injected run unexpectedly completed")

    status_after_crash = run_cli(python, ["status", *common])
    require_ok(status_after_crash, "crash status")
    crash_status = json.loads(status_after_crash.stdout)
    processing_before = crash_status["status"]["processing_batches"]
    if processing_before != 1:
        raise RuntimeError(
            f"expected one processing batch after crash, got {processing_before}"
        )

    resume = run_cli(
        python,
        [
            "resume",
            *common,
            "--manifest",
            str(SAMPLES / "manifest.json"),
        ],
    )
    require_ok(resume, "resume")
    resume_payload = json.loads(resume.stdout)

    status_path = run_dir / "status.json"
    verify_path = run_dir / "verify.json"
    result = run_cli(
        python,
        ["status", *common, "--with-groups", "--output", str(status_path)],
    )
    require_ok(result, "crash status final")
    result = run_cli(
        python,
        [
            "verify",
            *common,
            "--manifest",
            str(SAMPLES / "manifest.json"),
            "--output",
            str(verify_path),
        ],
    )
    require_ok(result, "crash verify")
    verification = json.loads(verify_path.read_text(encoding="utf-8"))
    return {
        "crash_exit": crash.returncode,
        "processing_before_resume": processing_before,
        "resume_ok": resume_payload["ok"],
        "resume_orphans_removed": resume_payload["orphans_removed"],
        "groups_verified": verification["groups_verified"],
        "records_verified": verification["records_verified"],
        "spill_files": verification["status"]["spill"]["files"],
        "readbacks": verification["status"]["readback"]["readbacks"],
        "state_hash": verification["state_hash"],
        "status_sha256": sha256_file(status_path),
        "verify_sha256": sha256_file(verify_path),
    }


def main() -> int:
    python = sys.executable
    ARTIFACTS.mkdir(parents=True, exist_ok=True)
    run1 = normal_run(python, ARTIFACTS / "demo_run_1")
    run2 = normal_run(python, ARTIFACTS / "demo_run_2")
    crash = crash_run(python, ARTIFACTS / "demo_crash_resume")

    names = ["load", "aggregate", "status", "verify"]
    byte_equal = {
        name: run1["output_sha256"][name] == run2["output_sha256"][name]
        for name in names
    }
    if not all(byte_equal.values()):
        raise RuntimeError(f"two demo runs differ: {byte_equal}")

    reference_status = ARTIFACTS / "demo_run_1" / "status.json"
    reference_verify = ARTIFACTS / "demo_run_1" / "verify.json"
    crash_status = ARTIFACTS / "demo_crash_resume" / "status.json"
    crash_verify = ARTIFACTS / "demo_crash_resume" / "verify.json"
    recovered_equal = {
        "status": sha256_file(reference_status) == sha256_file(crash_status),
        "verify": sha256_file(reference_verify) == sha256_file(crash_verify),
    }
    if not all(recovered_equal.values()):
        raise RuntimeError(f"crash recovery differs from reference: {recovered_equal}")

    summary = {
        "byte_identical_runs": byte_equal,
        "crash_resume": crash,
        "recovered_equal_to_reference": recovered_equal,
        "run1": run1,
        "run2": run2,
        "seed": 20261122,
    }
    summary_path = ARTIFACTS / "demo_summary.json"
    summary_path.write_text(
        json.dumps(summary, sort_keys=True, indent=2) + "\n",
        encoding="utf-8",
    )
    print(json.dumps(summary, sort_keys=True, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

