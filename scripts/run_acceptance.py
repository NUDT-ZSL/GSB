#!/usr/bin/env python3
"""Run the local fixed-seed acceptance workflow and emit a JSON report."""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from diagnostic_engine.atomicio import atomic_write_json
from diagnostic_engine.cache import build_frame_cache_key
from diagnostic_engine.checkpoint import CommitLock, CheckpointStore
from diagnostic_engine.engine import DiagnosticEngine, arrays_byte_equal
from diagnostic_engine.errors import ErrorJournal, DiagnosticError
from diagnostic_engine.hashing import array_content_hash
from diagnostic_engine.invalidation import plan_change, validate_recompute_reuse_partition
from diagnostic_engine.models import ClipTask, validate_manifest_records, validate_noise_array
from diagnostic_engine.propagation import generate_continuous
from diagnostic_engine.resources import ResourceLimits, plan_resources
from diagnostic_engine.sample import generate_sample, sample_tasks


WORKSPACE = ROOT / "workspace"
ACCEPTANCE_DIR = WORKSPACE / "acceptance"
REPORT_PATH = WORKSPACE / "diagnostic_report.json"


def _task_by_id(clip_id: str) -> ClipTask:
    return next(task for task in sample_tasks() if task.clip_id == clip_id)


def _pythonpath_env() -> dict[str, str]:
    env = os.environ.copy()
    env["PYTHONPATH"] = str(ROOT) + os.pathsep + env.get("PYTHONPATH", "")
    return env


def _run_cli(*arguments: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-m", "diagnostic_engine.cli", *arguments],
        cwd=ROOT,
        env=_pythonpath_env(),
        capture_output=True,
        text=True,
    )


def run_sigkill_recovery() -> dict[str, Any]:
    sample_info = generate_sample(ROOT / "samples")
    state_dir = ACCEPTANCE_DIR / "sigkill_state"
    report_path = ACCEPTANCE_DIR / "sigkill_resume_report.json"
    task = _task_by_id("clip_alpha")
    baseline = generate_continuous(task)

    killed = _run_cli(
        "run",
        "--manifest",
        sample_info["manifest"],
        "--state-dir",
        str(state_dir),
        "--clip-id",
        task.clip_id,
        "--terminate-after-frame",
        "5",
        "--sigkill",
    )
    checkpoint_before = json.loads(
        (state_dir / "checkpoints" / f"{task.clip_id}.json").read_text()
    )

    resumed = _run_cli(
        "run",
        "--manifest",
        sample_info["manifest"],
        "--state-dir",
        str(state_dir),
        "--clip-id",
        task.clip_id,
        "--report",
        str(report_path),
    )
    if resumed.returncode != 0:
        raise AssertionError(f"resume CLI failed: {resumed.stdout}\n{resumed.stderr}")

    resume_report = json.loads(report_path.read_text())
    clip_report = resume_report["clips"][0]
    engine = DiagnosticEngine(state_dir)
    recovered = engine.load_complete_frame_array(task)
    identical = arrays_byte_equal(recovered, baseline)
    if not identical:
        raise AssertionError("recovered frames are not byte-identical to baseline")
    self_check = engine.self_check(task.clip_id)

    return {
        "sigkill_returncode": killed.returncode,
        "sigkill_was_force_terminated": killed.returncode != 0,
        "checkpoint_before_resume_version": checkpoint_before["version"],
        "checkpoint_before_resume_next_frame": checkpoint_before["next_frame"],
        "byte_identical_to_continuous_run": identical,
        "baseline_array_sha256": array_content_hash(baseline),
        "recovered_array_sha256": array_content_hash(recovered),
        "resume_clip_report": clip_report,
        "self_check": self_check,
    }


def run_full_manifest() -> dict[str, Any]:
    state_dir = ACCEPTANCE_DIR / "full_state"
    report_path = ACCEPTANCE_DIR / "full_manifest_report.json"
    result = _run_cli(
        "run",
        "--manifest",
        str(ROOT / "samples" / "tasks.jsonl"),
        "--state-dir",
        str(state_dir),
        "--report",
        str(report_path),
    )
    if result.returncode != 0:
        raise AssertionError(f"full manifest run failed: {result.stdout}\n{result.stderr}")
    payload = json.loads(report_path.read_text())
    self_check = DiagnosticEngine(state_dir).self_check()
    payload["self_check"] = self_check
    return payload


def run_invalidation() -> dict[str, Any]:
    state_dir = ACCEPTANCE_DIR / "invalidation_state"
    old = _task_by_id("clip_alpha")
    engine = DiagnosticEngine(state_dir)
    initial = engine.run_clip(old)

    revised = dict(old.to_dict())
    revised["revision"] = old.revision + 1
    revised["change_frame"] = 5
    new = ClipTask.from_dict(revised)
    plan = plan_change(old, new)
    report = engine.run_clip(new, change_plan=plan, old_task=old)
    validate_recompute_reuse_partition(
        new.clip_id, new.total_frames, report["recomputed_frames"], report["reused_frames"]
    )
    self_check = engine.self_check(new.clip_id)
    return {
        "plan": plan.to_dict(),
        "initial_checkpoint_version": initial["checkpoint_version"],
        "final_checkpoint_version": report["checkpoint_version"],
        "recomputed_frames": report["recomputed_frames"],
        "reused_frames": report["reused_frames"],
        "noise_generated_frames": report["noise_generated_frames"],
        "partition_covers_all_frames_without_overlap": True,
        "clip_report": report,
        "self_check": self_check,
    }


def run_error_matrix(journal: ErrorJournal) -> list[dict[str, Any]]:
    checks: list[dict[str, Any]] = []

    def expect_code(scenario: str, expected_code: str, call: Any) -> None:
        try:
            call()
        except DiagnosticError as error:
            ok = error.code == expected_code
            checks.append(
                {
                    "scenario": scenario,
                    "expected_code": expected_code,
                    "actual_code": error.code,
                    "clip_id": error.clip_id,
                    "field": error.field,
                    "ok": ok,
                }
            )
            journal.record(error)
            if not ok:
                raise AssertionError(f"{scenario}: expected {expected_code}, got {error.code}")
            return
        raise AssertionError(f"{scenario}: expected {expected_code}, no error raised")

    def propagation_invalid() -> None:
        ClipTask.from_dict({**_task_by_id("clip_alpha").to_dict(), "block_size": 0})

    def duplicate() -> None:
        record = _task_by_id("clip_alpha").to_dict()
        result = validate_manifest_records([record, dict(record)])
        result.raise_if_invalid()

    def noise_shape() -> None:
        task = _task_by_id("clip_beta")
        validate_noise_array(task, np.zeros((2, 2, 2), dtype=np.uint8))

    def revision_rollback() -> None:
        state = ACCEPTANCE_DIR / "error_revision_state"
        engine = DiagnosticEngine(state)
        engine.run_clip(_task_by_id("clip_gamma"))
        rolled = ClipTask.from_dict(
            {**_task_by_id("clip_gamma").to_dict(), "revision": 0}
        )
        engine.run_clip(rolled)

    def checkpoint_conflict() -> None:
        store = CheckpointStore(ACCEPTANCE_DIR / "error_checkpoint")
        store.commit("c", {"clip_id": "c", "version": 1}, 0)
        store.commit("c", {"clip_id": "c", "version": 1}, 0)

    def recovery_mismatch() -> None:
        state = ACCEPTANCE_DIR / "error_recovery"
        task = ClipTask.from_dict(
            {
                "clip_id": "recovery_error",
                "total_frames": 8,
                "frame_shape": [4, 4],
                "noise_seed": 9,
                "motion_mode": "diffuse",
                "block_size": 4,
                "propagation_steps": 1,
                "revision": 1,
            }
        )
        engine = DiagnosticEngine(state)
        engine.run_clip(task)
        checkpoint = engine.checkpoints.load(task.clip_id)
        key_path = engine.cache.key_path(checkpoint["cache_keys"]["0"])
        key_path.unlink()
        DiagnosticEngine(state).run_clip(task)

    def cache_key_invalid() -> None:
        build_frame_cache_key({"clip_id": "x", "upstream_hash": ""})

    def invalidation_mismatch() -> None:
        validate_recompute_reuse_partition("x", 3, [0, 1], [1])

    def state_inconsistent() -> None:
        state = ACCEPTANCE_DIR / "error_state_inconsistent"
        task = _task_by_id("clip_gamma")
        engine = DiagnosticEngine(state)
        engine.run_clip(task)
        path = engine.checkpoints.path_for(task.clip_id)
        payload = json.loads(path.read_text())
        payload["frame_hashes"]["0"] = "f" * 64
        atomic_write_json(path, payload)
        engine.self_check(task.clip_id)

    def stage_busy() -> None:
        lock = CommitLock(ACCEPTANCE_DIR / "error_stage_busy")
        lock.acquire_nonblocking()
        try:
            CommitLock(ACCEPTANCE_DIR / "error_stage_busy").acquire_nonblocking()
        finally:
            lock.release()

    def resource_limit() -> None:
        plan_resources(
            _task_by_id("clip_alpha"),
            ResourceLimits(max_blocks_per_clip=1),
        )

    expect_code("non_positive_block_size", "E_PROPAGATION_INVALID", propagation_invalid)
    expect_code("duplicate_clip_id", "E_DUPLICATE_CLIP_ID", duplicate)
    expect_code("noise_array_shape", "E_NOISE_SHAPE_MISMATCH", noise_shape)
    expect_code("revision_rollback", "E_REVISION_ROLLBACK", revision_rollback)
    expect_code("stale_checkpoint_commit", "E_CHECKPOINT_CONFLICT", checkpoint_conflict)
    expect_code("missing_committed_cache", "E_RECOVERY_MISMATCH", recovery_mismatch)
    expect_code("missing_upstream_hash", "E_CACHE_KEY_INVALID", cache_key_invalid)
    expect_code("bad_invalidation_partition", "E_INVALIDATION_MISMATCH", invalidation_mismatch)
    expect_code("corrupt_state", "E_STATE_INCONSISTENT", state_inconsistent)
    expect_code("concurrent_commit", "E_STAGE_BUSY", stage_busy)
    expect_code("block_count_resource_limit", "E_RESOURCE_LIMIT", resource_limit)
    return checks


def run_unit_tests() -> dict[str, Any]:
    completed = subprocess.run(
        [sys.executable, "-m", "unittest", "discover", "-s", "tests"],
        cwd=ROOT,
        capture_output=True,
        text=True,
    )
    output = (completed.stdout + completed.stderr).strip()
    return {
        "command": "python3 -m unittest discover -s tests",
        "returncode": completed.returncode,
        "passed": completed.returncode == 0,
        "tail": output[-3000:],
    }


def main() -> int:
    if ACCEPTANCE_DIR.exists():
        shutil.rmtree(ACCEPTANCE_DIR)
    ACCEPTANCE_DIR.mkdir(parents=True, exist_ok=True)
    WORKSPACE.mkdir(parents=True, exist_ok=True)
    journal = ErrorJournal(ACCEPTANCE_DIR / "error_journal")

    recovery = run_sigkill_recovery()
    full_manifest = run_full_manifest()
    invalidation = run_invalidation()
    error_checks = run_error_matrix(journal)
    tests = run_unit_tests()

    all_error_checks_ok = all(item["ok"] for item in error_checks)
    ok = bool(
        recovery["byte_identical_to_continuous_run"]
        and full_manifest["ok"]
        and invalidation["partition_covers_all_frames_without_overlap"]
        and all_error_checks_ok
        and tests["passed"]
    )
    report = {
        "schema": "diagnostic-acceptance-report/v1",
        "ok": ok,
        "python": sys.version.split()[0],
        "numpy": np.__version__,
        "sample_manifest": str(ROOT / "samples" / "tasks.jsonl"),
        "recovery_report": recovery,
        "checkpoint_versions": {
            clip["clip_id"]: clip["checkpoint_version"]
            for clip in full_manifest["clips"]
        },
        "frame_checksums": {
            clip["clip_id"]: clip["frame_checksums"]
            for clip in full_manifest["clips"]
        },
        "recomputed_frames": invalidation["recomputed_frames"],
        "reused_frames": invalidation["reused_frames"],
        "full_manifest_report_path": str(
            ACCEPTANCE_DIR / "full_manifest_report.json"
        ),
        "full_manifest_clips": full_manifest["clips"],
        "invalidation_report": invalidation,
        "error_code_checks": error_checks,
        "unit_tests": tests,
        "latest_error": journal.latest(),
    }
    atomic_write_json(REPORT_PATH, report)
    print(json.dumps({"ok": ok, "report": str(REPORT_PATH)}, sort_keys=True))
    if not ok:
        print(json.dumps(report, sort_keys=True, indent=2)[:10000])
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
