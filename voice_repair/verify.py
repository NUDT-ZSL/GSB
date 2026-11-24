from __future__ import annotations

import shutil
from pathlib import Path
from typing import Any

import numpy as np

from .audio import atomic_write_json, read_wav_interval
from .engine import BatchEngine, RunOptions
from .report import deterministic_report_view
from .task import ValidationResult


def run_verification(
    *,
    validation: ValidationResult,
    work_dir: Path,
    workers: int,
) -> dict[str, Any]:
    if validation.task is None:
        raise ValueError("valid task is required for verification")
    task = validation.task
    work_dir.mkdir(parents=True, exist_ok=True)
    runs = {}
    for name, worker_count in (("serial", 1), ("parallel", max(2, workers))):
        run_dir = work_dir / name
        if run_dir.exists():
            shutil.rmtree(run_dir)
        run_dir.mkdir(parents=True)
        state_dir = run_dir / "state"
        engine = BatchEngine(
            task=task,
            validation_issues=validation.issues,
            ledger_path=state_dir / "ledger.sqlite",
            output_dir=state_dir / "output",
            report_path=state_dir / "report.json",
            state_dir=state_dir,
            options=RunOptions(mode=name, workers=worker_count),
        )
        outcome = engine.run()
        runs[name] = outcome

    serial = runs["serial"]
    parallel = runs["parallel"]
    serial_view = deterministic_report_view(serial.report)
    parallel_view = deterministic_report_view(parallel.report)
    deterministic_equal = serial_view == parallel_view
    schedule_equal = serial_view.get("scheduling") == parallel_view.get("scheduling")
    ledger_digests_equal = _ledger_digests(serial_view) == _ledger_digests(parallel_view)

    output_digest_equal = True
    output_bytes_equal = True
    sample_exact_equal = True
    max_sample_delta = 0.0
    compared_outputs = 0
    serial_recordings = {
        item["recording_id"]: item for item in serial.report.get("recordings", [])
    }
    parallel_recordings = {
        item["recording_id"]: item for item in parallel.report.get("recordings", [])
    }
    if set(serial_recordings) != set(parallel_recordings):
        output_digest_equal = False
        output_bytes_equal = False
        sample_exact_equal = False
    else:
        for recording_id in sorted(serial_recordings):
            left = serial_recordings[recording_id]
            right = parallel_recordings[recording_id]
            if left.get("output_sha256") != right.get("output_sha256"):
                output_digest_equal = False
            left_path = left.get("output_path")
            right_path = right.get("output_path")
            if not left_path or not right_path:
                continue
            compared_outputs += 1
            left_bytes = Path(left_path).read_bytes()
            right_bytes = Path(right_path).read_bytes()
            if left_bytes != right_bytes:
                output_bytes_equal = False
            left_samples = read_wav_interval(Path(left_path), 0, left["sample_count"])
            right_samples = read_wav_interval(Path(right_path), 0, right["sample_count"])
            if left_samples.size != right_samples.size:
                sample_exact_equal = False
                continue
            if left_samples.size:
                delta = float(np.max(np.abs(left_samples - right_samples)))
                max_sample_delta = max(max_sample_delta, delta)

    checks = {
        "deterministic_report_equal": deterministic_equal,
        "output_digest_equal": output_digest_equal,
        "output_bytes_equal": output_bytes_equal,
        "sample_exact_equal": sample_exact_equal,
        "ledger_block_digests_equal": ledger_digests_equal,
        "schedule_equal": schedule_equal,
    }
    ok = all(checks.values()) and serial.exit_code == 0 and parallel.exit_code == 0
    result = {
        "schema_version": 1,
        "ok": ok,
        "task_id": task.task_id,
        "task_sha256": task.raw_sha256,
        "declared_parallelism": task.parallelism,
        "serial_workers": 1,
        "parallel_workers": max(2, workers),
        "compared_outputs": compared_outputs,
        "max_abs_sample_delta": max_sample_delta,
        "checks": checks,
        "serial": {
            "exit_code": serial.exit_code,
            "status": serial.status,
            "report": str(serial.report_path),
        },
        "parallel": {
            "exit_code": parallel.exit_code,
            "status": parallel.status,
            "report": str(parallel.report_path),
        },
        "verification_path": str(work_dir / "verification.json"),
        "deterministic_view": serial_view if deterministic_equal else None,
    }
    atomic_write_json(work_dir / "verification.json", result)
    return result


def _ledger_digests(view: dict[str, Any]) -> list[tuple]:
    return [
        (
            recording["recording_id"],
            stage["stage"],
            tuple(stage["block_sha256"]),
        )
        for recording in view["recordings"]
        for stage in recording["stages"]
    ]
