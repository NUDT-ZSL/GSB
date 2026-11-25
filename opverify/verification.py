from __future__ import annotations

import json
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any

from .consistency import run_self_check
from .engine import evaluate_job
from .errors import OpVerifyError
from .hashing import sha256_bytes
from .ledger import CommitLock
from .samples import base_sample_tasks, generate_sample_set, read_jsonl
from .service import VerificationService
from .snapshots import SnapshotStore, make_envelope
from .verify import compare_outputs


PROJECT_ROOT = Path(__file__).resolve().parents[1]


def _compact_run(report: dict[str, Any]) -> dict[str, Any]:
    return {
        "ok": report.get("ok"),
        "ledger_version": report.get("ledger_version"),
        "trigger_revision": report.get("trigger_revision"),
        "recompute": report.get("recompute"),
        "reused_for_focus": [
            item
            for item in report.get("reused", [])
            if item["job_id"]
            in {"inc_block", "inc_block_down", "inc_geom", "inc_geom_down", "inc_shape", "inc_shape_down"}
        ],
        "recompute_jobs": report.get("recompute_jobs"),
        "fully_reused_jobs_count": len(report.get("fully_reused_jobs", [])),
        "propagation": report.get("propagation"),
        "snapshot_checksums": report.get("snapshot_checksums"),
    }


def _file_tree_digests(root: Path) -> dict[str, str]:
    result: dict[str, str] = {}
    for path in sorted(p for p in root.rglob("*") if p.is_file()):
        result[str(path.relative_to(root))] = sha256_bytes(path.read_bytes())
    return result


def _real_kill_and_resume(sample: Path, workspace: Path) -> dict[str, Any]:
    crash_root = workspace / "recovery-crash"
    one_shot_root = workspace / "recovery-oneshot"
    manifest = sample / "tasks_v1.jsonl"

    one_shot = VerificationService(one_shot_root)
    one_shot_report = one_shot.run_manifest(manifest, batch_id="full")

    cmd_prefix = [sys.executable, "-m", "opverify.cli"]
    crash_cmd = cmd_prefix + [
        "run",
        "--root",
        str(crash_root),
        "--manifest",
        str(manifest),
        "--batch-id",
        "full",
        "--crash-after-commits",
        "3",
    ]
    crash_process = subprocess.run(
        crash_cmd, cwd=PROJECT_ROOT, capture_output=True, text=True, check=False
    )

    resume_cmd = cmd_prefix + [
        "run",
        "--root",
        str(crash_root),
        "--manifest",
        str(manifest),
        "--batch-id",
        "full",
    ]
    resume_process = subprocess.run(
        resume_cmd, cwd=PROJECT_ROOT, capture_output=True, text=True, check=False
    )
    try:
        resume_report = json.loads(resume_process.stdout)
    except json.JSONDecodeError:
        resume_report = {"ok": False, "stdout": resume_process.stdout, "stderr": resume_process.stderr}

    checksum_match = (
        resume_report.get("snapshot_checksums") == one_shot_report.get("snapshot_checksums")
    )
    self_check = run_self_check(crash_root, base_dir=sample)
    return {
        "crash_returncode": crash_process.returncode,
        "crash_signal_expected": crash_process.returncode == -9,
        "resume_returncode": resume_process.returncode,
        "resume_ok": bool(resume_report.get("ok")),
        "skipped_chunk_jobs": [
            item["job_id"]
            for item in resume_report.get("execution", {}).get("skipped_committed", [])
            if item["stage"] == "chunk"
        ],
        "computed_chunk_jobs": [
            item["job_id"]
            for item in resume_report.get("execution", {}).get("computed", [])
            if item["stage"] == "chunk"
        ],
        "checksum_match_one_shot": checksum_match,
        "one_shot_checksums": one_shot_report.get("snapshot_checksums"),
        "resume_checksums": resume_report.get("snapshot_checksums"),
        "self_check_ok": self_check["ok"],
    }


def _concurrency_result(workspace: Path) -> dict[str, Any]:
    lock_path = workspace / "concurrency" / "commit.lock"
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    holder_ready = threading.Event()
    release_holder = threading.Event()
    holder_error: list[BaseException] = []

    def hold_lock() -> None:
        try:
            lock = CommitLock.acquire(lock_path)
            holder_ready.set()
            release_holder.wait(timeout=5.0)
            lock.release()
        except BaseException as exc:  # pragma: no cover - diagnostic path
            holder_error.append(exc)
            holder_ready.set()

    thread = threading.Thread(target=hold_lock, daemon=True)
    thread.start()
    holder_ready.wait(timeout=2.0)

    started = time.perf_counter()
    busy_code = None
    try:
        CommitLock.acquire(lock_path)
    except OpVerifyError as exc:
        busy_code = exc.code
    elapsed_ms = (time.perf_counter() - started) * 1000.0
    release_holder.set()
    thread.join(timeout=2.0)

    after = CommitLock.acquire(lock_path)
    after.release()
    return {
        "first_holder_thread": "ACQUIRED",
        "late_comer_code": busy_code,
        "late_comer_wait_ms": round(elapsed_ms, 4),
        "after_release": "ACQUIRED",
        "holder_error": [str(item) for item in holder_error],
        "ok": busy_code == "E_STAGE_BUSY" and not holder_error,
    }


def _validation_error_sample(sample: Path) -> dict[str, Any]:
    from .engine import run_unified

    cases = read_jsonl(sample / "invalid_cases.jsonl")
    results = []
    for case in cases:
        response = run_unified(
            case["record"], base_dir=sample, prior_revision=case.get("prior_revision")
        )
        actual = response.get("error", {}).get("code") if not response.get("ok") else "OK"
        results.append(
            {
                "case": case["case"],
                "expected": case["expect_code"],
                "actual": actual,
                "passed": actual == case["expect_code"],
            }
        )
    return {"ok": all(item["passed"] for item in results), "results": results}


def _byte_reproducibility(sample: Path) -> dict[str, Any]:
    records = read_jsonl(sample / "tasks_v1.jsonl")
    focus = {"conv_basic", "interp_nearest", "interp_bilinear", "interp_bicubic"}
    results = []
    for record in records:
        if record["job_id"] not in focus:
            continue
        first = evaluate_job(record, base_dir=sample)
        second = evaluate_job(record, base_dir=sample)
        reference_same = first["reference"].tobytes() == second["reference"].tobytes()
        output_same = first["output"].tobytes() == second["output"].tobytes()
        results.append(
            {
                "job_id": record["job_id"],
                "reference_bytes": first["reference"].nbytes,
                "reference_byte_identical": reference_same,
                "chunk_byte_identical": output_same,
            }
        )
    return {"ok": all(x["reference_byte_identical"] and x["chunk_byte_identical"] for x in results), "jobs": results}


def _tolerance_result() -> dict[str, Any]:
    record = next(item for item in base_sample_tasks() if item["job_id"] == "conv_basic")
    result = evaluate_job(record)
    corrupted = result["output"].copy()
    corrupted[0, 0, 2, 3] += 0.5
    try:
        compare_outputs(result["reference"], corrupted)
    except OpVerifyError as exc:
        return {"ok": exc.code == "E_TOLERANCE_EXCEEDED", **exc.to_dict()["error"]}
    return {"ok": False, "code": "NO_ERROR"}


def run_verification(workspace: str | Path) -> dict[str, Any]:
    root = Path(workspace)
    if root.exists():
        shutil.rmtree(root)
    root.mkdir(parents=True)

    sample = root / "sample"
    generate_sample_set(sample)
    sample_repeat = root / "sample-repeat"
    generate_sample_set(sample_repeat)
    deterministic_samples = _file_tree_digests(sample) == _file_tree_digests(sample_repeat)

    validation = _validation_error_sample(sample)
    reproducibility = _byte_reproducibility(sample)
    tolerance = _tolerance_result()

    incremental_root = root / "incremental"
    service = VerificationService(incremental_root)
    full_report = service.run_manifest(sample / "tasks_v1.jsonl", batch_id="full")
    full_check = run_self_check(incremental_root, base_dir=sample)

    block_report = service.run_manifest(
        sample / "update_block_policy.jsonl", batch_id="block-r2"
    )
    geometry_report = service.run_manifest(
        sample / "update_geometry.jsonl", batch_id="geometry-r2"
    )
    shape_report = service.run_manifest(sample / "update_shape.jsonl", batch_id="shape-r2")
    final_check = run_self_check(incremental_root, base_dir=sample)

    rollback_code = None
    old_block = next(item for item in base_sample_tasks() if item["job_id"] == "inc_block")
    old_block["revision"] = 1
    try:
        service.run_records([old_block], batch_id="rollback-attempt")
    except OpVerifyError as exc:
        rollback_code = exc.code

    snapshot_root = root / "snapshot-rollback"
    snapshot_store = SnapshotStore(snapshot_root)
    snapshot_store.commit_bundle([make_envelope("ledger", 2, {"v": 2})])
    snapshot_rollback_code = None
    try:
        snapshot_store.replace_current(make_envelope("ledger", 1, {"v": 1}))
    except OpVerifyError as exc:
        snapshot_rollback_code = exc.code

    recovery = _real_kill_and_resume(sample, root)
    concurrency = _concurrency_result(root)

    summary = {
        "ok": all(
            [
                deterministic_samples,
                validation["ok"],
                reproducibility["ok"],
                tolerance["ok"],
                full_report["ok"],
                full_check["ok"],
                block_report["ok"],
                geometry_report["ok"],
                shape_report["ok"],
                final_check["ok"],
                rollback_code == "E_REVISION_ROLLBACK",
                snapshot_rollback_code == "E_SNAPSHOT_ROLLBACK",
                recovery["resume_ok"],
                recovery["checksum_match_one_shot"],
                recovery["self_check_ok"],
                recovery["crash_signal_expected"],
                concurrency["ok"],
            ]
        ),
        "deterministic_sample_generation": deterministic_samples,
        "validation_errors": validation,
        "byte_reproducibility": reproducibility,
        "tolerance_failure_demo": tolerance,
        "full_run": {
            "ledger_version": full_report["ledger_version"],
            "job_count": len(full_report["recompute_jobs"]),
            "self_check_ok": full_check["ok"],
        },
        "incremental": {
            "block_policy": _compact_run(block_report),
            "geometry": _compact_run(geometry_report),
            "shape": _compact_run(shape_report),
        },
        "revision_rollback_code": rollback_code,
        "snapshot_rollback_code": snapshot_rollback_code,
        "recovery": recovery,
        "concurrency": concurrency,
        "final_self_check": final_check,
    }
    report_path = root / "verification_report.json"
    report_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    artifact_copy = PROJECT_ROOT / "artifacts" / "opverify_verification_report.json"
    artifact_copy.parent.mkdir(exist_ok=True)
    artifact_copy.write_text(json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    summary["report_path"] = str(report_path)
    summary["artifact_copy"] = str(artifact_copy)
    return summary
