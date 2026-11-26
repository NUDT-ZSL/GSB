from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

from .adjudicate import adjudicate_from_disk
from .hashing import read_json, write_json_atomic
from .injections import run_injection_suite
from .pipeline import Pipeline
from .sample import FIXED_SEED, copy_case_for_run, generate_sample_case, generate_task_workspace


REPO_ROOT = Path(__file__).resolve().parents[1]


def verify_byte_determinism(root: Path) -> dict[str, Any]:
    first = root / "determinism-a"
    second = root / "determinism-b"
    generate_sample_case(first, case_id="case-byte", seed=FIXED_SEED)
    generate_sample_case(second, case_id="case-byte", seed=FIXED_SEED)
    first_bytes = (first / "initial_csr.json").read_bytes()
    second_bytes = (second / "initial_csr.json").read_bytes()
    return {
        "same_initial_csr_bytes": first_bytes == second_bytes,
        "initial_csr_sha256": read_json(first / "initial_csr.json")["content_hash"],
        "byte_count": len(first_bytes),
    }


def _cli_environment() -> dict[str, str]:
    env = dict(os.environ)
    existing = env.get("PYTHONPATH", "")
    env["PYTHONPATH"] = str(REPO_ROOT) + (os.pathsep + existing if existing else "")
    return env


def run_demo(root: str | Path) -> dict[str, Any]:
    root = Path(root)
    root.mkdir(parents=True, exist_ok=True)
    samples_root = root / "samples"
    task_manifest = generate_task_workspace(samples_root, case_count=2, seed=FIXED_SEED)
    source_case = Path(task_manifest["cases"][0]["path"])

    determinism = verify_byte_determinism(root / "determinism")
    if not determinism["same_initial_csr_bytes"]:
        raise AssertionError("fixed-seed CSR generation is not byte deterministic")

    normal_run = root / "normal-run" / task_manifest["cases"][0]["case_id"]
    copy_case_for_run(source_case, normal_run)
    normal_pipeline = Pipeline(normal_run)
    normal_v1 = normal_pipeline.run_through(1)
    normal_v2 = normal_pipeline.run_through(2)
    adjudication = adjudicate_from_disk(normal_run)
    write_json_atomic(normal_run / "committed" / "reports" / "adjudication-standalone.json", adjudication)

    recovery_run = root / "recovery-run" / task_manifest["cases"][0]["case_id"]
    copy_case_for_run(source_case, recovery_run)
    Pipeline(recovery_run).run_through(1)
    crash_command = [
        sys.executable,
        "-m",
        "truth_ledger.cli",
        "pipeline",
        "--case-dir",
        str(recovery_run),
        "--target-version",
        "2",
        "--crash-at",
        "recompute_after_first_new",
    ]
    crashed = subprocess.run(
        crash_command,
        cwd=REPO_ROOT,
        env=_cli_environment(),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    pre_recovery_latest = read_json(recovery_run / "state" / "latest.json")
    recovery_result = Pipeline(recovery_run).run_through(2)

    injection_summary = run_injection_suite(root / "injections")
    report = {
        "ok": True,
        "root": str(root),
        "task_manifest": str(samples_root / "task_manifest.json"),
        "determinism": determinism,
        "normal_run": {
            "case_dir": str(normal_run),
            "v1": normal_v1,
            "v2": normal_v2,
            "adjudication": {
                key: value
                for key, value in adjudication.items()
                if key not in ("views", "intervals")
            },
        },
        "recovery": {
            "case_dir": str(recovery_run),
            "crash_command": " ".join(crash_command),
            "crash_returncode": crashed.returncode,
            "crash_stderr_tail": crashed.stderr[-500:],
            "pre_recovery_snapshot_version": pre_recovery_latest["snapshot_version"],
            "pre_recovery_stage": pre_recovery_latest["stage"],
            "result": recovery_result,
        },
        "injections": {
            "total": injection_summary["total"],
            "passed_count": injection_summary["passed_count"],
            "failed_count": injection_summary["failed_count"],
            "records_path": str(root / "injections" / "injection_records.json"),
        },
    }
    report_path = root / "demo_report.json"
    write_json_atomic(report_path, report)
    return {"report_path": str(report_path), **report}


def main() -> None:
    result = run_demo(REPO_ROOT / "artifacts" / "demo")
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
