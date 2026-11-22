"""Command-line entry points for generation, runs, verification, and demos."""

from __future__ import annotations

import argparse
import json
import os
import shutil
import signal
import subprocess
import sys
import threading
from pathlib import Path
from typing import Any

from .config import PipelineConfig
from .dataset import generate_dataset
from .deterministic import (
    atomic_write_bytes,
    atomic_write_json,
    canonical_bytes,
    checksum_without,
    read_json_file,
    sha256_bytes,
)
from .errors import BusinessError
from .pipeline import Pipeline, RunOutcome
from .snapshots import SnapshotStore, resign_snapshot
from .verification import (
    compare_recovery,
    verify_checkpoint,
    verify_final_result,
)


PROJECT_ROOT = Path(__file__).resolve().parent.parent


def _config_from_args(args: argparse.Namespace) -> PipelineConfig:
    return PipelineConfig(
        history_window_length=args.history_window,
        decay=args.decay,
        observation_ratio=args.observation_ratio,
        max_frames_per_clip=args.max_frames_per_clip,
        max_resident_history_windows=args.max_resident_history_windows,
        max_parallel_clips=args.max_parallel_clips,
        max_history_window_length=args.max_history_window_length,
        resource_policy=args.resource_policy,
        hit_distance_px=args.hit_distance_px,
    )


def _finalize_report(report: dict[str, Any]) -> dict[str, Any]:
    report["report_checksum"] = checksum_without(report, "report_checksum")
    return report


def _write_report(path: Path, report: dict[str, Any]) -> None:
    _finalize_report(report)
    atomic_write_json(path, report)


def _print_json(value: Any) -> None:
    sys.stdout.write(canonical_bytes(value).decode("utf-8") + "\n")
    sys.stdout.flush()


def _error_only(exc: BusinessError) -> dict[str, Any]:
    return exc.to_dict()


def _inject_manifest_fault(
    manifest_path: Path, fault: str, workdir: Path
) -> tuple[Path, dict[str, Any]]:
    records: list[dict[str, Any]] = []
    with manifest_path.open("rb") as handle:
        for line in handle:
            if line.strip():
                records.append(json.loads(line))
    if not records:
        raise BusinessError(
            "E_MANIFEST_INVALID", "manifest contains no clips", field="manifest"
        )
    first = dict(records[0])
    if fault == "duplicate_clip_id":
        records.insert(1, dict(first))
        mutated = ["clip_id", first["clip_id"]]
    elif fault == "frame_count_mismatch":
        first["frame_count"] = int(first["frame_count"]) + 1
        records[0] = first
        mutated = ["frame_count", first["frame_count"]]
    elif fault == "unknown_temporal_policy":
        first["temporal_policy"] = "not_a_policy"
        records[0] = first
        mutated = ["temporal_policy", first["temporal_policy"]]
    elif fault == "frame_interval_nonpositive":
        first["frame_interval_ms"] = 0
        records[0] = first
        mutated = ["frame_interval_ms", 0]
    elif fault == "motion_mode_unknown":
        first["motion_mode"] = "teleport"
        records[0] = first
        mutated = ["motion_mode", first["motion_mode"]]
    elif fault == "resource_limit":
        mutated = ["frame_count", first["frame_count"]]
    else:
        raise BusinessError(
            "E_CONFIG_INVALID", "unknown fault injection type", field="inject_fault"
        )
    fault_dir = workdir / "faults"
    fault_dir.mkdir(parents=True, exist_ok=True)
    fault_manifest = fault_dir / "manifest.jsonl"
    data = b"".join(canonical_bytes(record) + b"\n" for record in records)
    atomic_write_bytes(fault_manifest, data)
    return fault_manifest, {
        "kind": fault,
        "mutated_field": mutated[0],
        "mutated_value": mutated[1],
        "manifest": "faults/manifest.jsonl",
    }


def _run_report_path(args: argparse.Namespace, status: str) -> Path:
    if args.report:
        return Path(args.report)
    name = "exit.json" if status == "stopped" else "run.json"
    return Path(args.workdir) / "reports" / name


def command_generate(args: argparse.Namespace) -> int:
    result = generate_dataset(args.out, seed=args.seed, clip_count=args.clips)
    _print_json(
        {
            "status": "generated",
            "dataset": args.out,
            "clip_count": result["clip_count"],
            "seed": result["seed"],
            "manifest_sha256": result["metadata"]["manifest_sha256"],
            "frames_sha256": result["metadata"]["frames_sha256"],
        }
    )
    return 0


def command_run(args: argparse.Namespace) -> int:
    config = _config_from_args(args)
    if args.inject_fault == "window_invalid":
        config = PipelineConfig(**{**config.as_dict(), "history_window_length": 0})
    if args.inject_fault == "decay_invalid":
        config = PipelineConfig(**{**config.as_dict(), "decay": 0.0})
    if args.inject_fault == "resource_limit":
        config = PipelineConfig(**{**config.as_dict(), "max_frames_per_clip": 1})
    manifest_path = Path(args.manifest)
    frames_path = Path(args.frames)
    fault_metadata: dict[str, Any] | None = None
    if args.inject_fault in {
        "duplicate_clip_id",
        "frame_count_mismatch",
        "unknown_temporal_policy",
        "frame_interval_nonpositive",
        "motion_mode_unknown",
    }:
        manifest_path, fault_metadata = _inject_manifest_fault(
            manifest_path, args.inject_fault, Path(args.workdir)
        )
    elif args.inject_fault == "resource_limit":
        fault_metadata = {
            "kind": "resource_limit",
            "mutated_field": "max_frames_per_clip",
            "mutated_value": 1,
        }
    elif args.inject_fault in {"window_invalid", "decay_invalid"}:
        fault_metadata = {
            "kind": args.inject_fault,
            "mutated_field": (
                "history_window_length"
                if args.inject_fault == "window_invalid"
                else "decay"
            ),
            "mutated_value": (
                0 if args.inject_fault == "window_invalid" else 0.0
            ),
        }
    stop_event = threading.Event()
    stop_signal: list[int] = []
    previous_handlers: dict[int, Any] = {}

    def _handle_signal(signum: int, _frame: Any) -> None:
        stop_signal.append(signum)
        stop_event.set()

    for signum in (signal.SIGINT, signal.SIGTERM):
        previous_handlers[signum] = signal.getsignal(signum)
        signal.signal(signum, _handle_signal)

    def _kill_after_stage(stage_index: int, _checkpoint: dict[str, Any]) -> None:
        if args.kill_after_stage and stage_index + 1 == args.kill_after_stage:
            sys.stdout.flush()
            sys.stderr.flush()
            os._exit(137)

    outcome: RunOutcome | None = None
    try:
        outcome = Pipeline(Path(args.workdir), config).run(
            manifest_path,
            frames_path,
            reset=args.reset,
            on_stage_committed=_kill_after_stage,
            stop_requested=stop_event.is_set,
        )
        status = outcome.status
        report_path = _run_report_path(args, status)
        report = dict(outcome.report)
        report["command"] = "run"
        report["ok"] = True
        report["exit_reason"] = "completed" if status == "completed" else "signal_boundary"
        report["fault_injection"] = fault_metadata
        if stop_signal:
            report["signal"] = stop_signal[0]
        report["report_path"] = str(report_path)
        _write_report(report_path, report)
        _print_json(
            {
                "status": status,
                "ok": True,
                "report": str(report_path),
                "final_result_sha256": outcome.report.get(
                    "final_result_sha256"
                ),
                "checkpoint_sha256": outcome.report.get("checkpoint_sha256"),
            }
        )
        return 0 if status == "completed" else 130
    except BusinessError as exc:
        status = "failed"
        report_path = _run_report_path(args, status)
        report = {
            "schema_version": 1,
            "command": "run",
            "status": status,
            "ok": False,
            "business_code": exc.code,
            "field": exc.field,
            "clip_id": exc.clip_id,
            "stage": exc.stage,
            "version": exc.version,
            "message": exc.message,
            "detail": exc.detail,
            "business_error": exc.to_dict(),
            "fault_injection": fault_metadata,
            "report_path": str(report_path),
        }
        _write_report(report_path, report)
        _print_json(report)
        return 2
    finally:
        for signum, previous in previous_handlers.items():
            signal.signal(signum, previous)


def command_verify(args: argparse.Namespace) -> int:
    default_report = Path(args.workdir) / "reports" / "verify.json"
    report_path = Path(args.report) if args.report else default_report
    try:
        store = SnapshotStore(Path(args.workdir))
        checkpoint = store.load()
        if checkpoint is None:
            raise BusinessError(
                "E_CROSS_STAGE_INVALID",
                "no committed checkpoint exists",
                stage="frame_sample",
            )
        cross_stage = verify_checkpoint(checkpoint)
        final_path = Path(args.workdir) / "final" / "result.json"
        final_report: dict[str, Any] = {
            "exists": final_path.exists(),
            "verified": False,
        }
        if final_path.exists():
            raw = final_path.read_bytes()
            final_result = json.loads(raw.decode("utf-8"))
            verify_final_result(final_result, checkpoint)
            final_report = {
                "exists": True,
                "verified": True,
                "sha256": sha256_bytes(raw),
                "result_checksum": final_result["result_checksum"],
            }
        report = {
            "schema_version": 1,
            "command": "verify",
            "status": "verified",
            "ok": True,
            "cross_stage": cross_stage,
            "final_result": final_report,
            "checkpoint_versions": checkpoint["version_history"],
        }
        report["report_path"] = str(report_path)
        _write_report(report_path, report)
        _print_json(report)
        return 0
    except BusinessError as exc:
        report = {
            "schema_version": 1,
            "command": "verify",
            "status": "failed",
            "ok": False,
            "business_code": exc.code,
            "business_error": exc.to_dict(),
            "report_path": str(report_path),
        }
        _write_report(report_path, report)
        _print_json(report)
        return 2


def command_compare(args: argparse.Namespace) -> int:
    report_path = (
        Path(args.report)
        if args.report
        else Path(args.recovery_workdir) / "reports" / "compare.json"
    )
    try:
        baseline_checkpoint = SnapshotStore(Path(args.baseline_workdir)).load()
        recovered_checkpoint = SnapshotStore(Path(args.recovery_workdir)).load()
        if baseline_checkpoint is None or recovered_checkpoint is None:
            raise BusinessError(
                "E_RECOVERY_MISMATCH",
                "both runs must have committed checkpoints",
                stage="frame_sample",
            )
        baseline_final_path = Path(args.baseline_workdir) / "final" / "result.json"
        recovered_final_path = Path(args.recovery_workdir) / "final" / "result.json"
        baseline_bytes = (
            baseline_final_path.read_bytes() if baseline_final_path.exists() else None
        )
        recovered_bytes = (
            recovered_final_path.read_bytes() if recovered_final_path.exists() else None
        )
        comparison = compare_recovery(
            baseline_checkpoint,
            recovered_checkpoint,
            reference_final_bytes=baseline_bytes,
            recovered_final_bytes=recovered_bytes,
        )
        comparison["checkpoint_bytes_equal"] = (
            SnapshotStore(Path(args.baseline_workdir)).snapshot_path.read_bytes()
            == SnapshotStore(Path(args.recovery_workdir)).snapshot_path.read_bytes()
        )
        report = {
            "schema_version": 1,
            "command": "compare",
            "status": "equal",
            "ok": True,
            **comparison,
            "report_path": str(report_path),
        }
        _write_report(report_path, report)
        _print_json(report)
        return 0
    except BusinessError as exc:
        report = {
            "schema_version": 1,
            "command": "compare",
            "status": "failed",
            "ok": False,
            "business_code": exc.code,
            "business_error": exc.to_dict(),
            "report_path": str(report_path),
        }
        _write_report(report_path, report)
        _print_json(report)
        return 2


def _demo_subprocess(arguments: list[str], capture_output: bool = True) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-m", "motion_pipeline", *arguments],
        cwd=str(PROJECT_ROOT),
        text=True,
        capture_output=capture_output,
        check=False,
    )


def _load_report(path: Path) -> dict[str, Any]:
    return read_json_file(path)


def _demo_cleanup(root: Path) -> None:
    for name in (
        "dataset",
        "baseline",
        "recovery",
        "rollback",
        "corrupt",
        "replace",
        "faults",
    ):
        shutil.rmtree(root / name, ignore_errors=True)


def command_demo(args: argparse.Namespace) -> int:
    root = Path(args.out)
    root.mkdir(parents=True, exist_ok=True)
    _demo_cleanup(root)
    dataset = root / "dataset"
    generate_dataset(dataset, seed=args.seed, clip_count=args.clips)
    baseline_workdir = root / "baseline"
    recovery_workdir = root / "recovery"
    baseline_report_path = baseline_workdir / "reports" / "run.json"
    killed_report_path = recovery_workdir / "reports" / "killed.json"
    recovery_report_path = recovery_workdir / "reports" / "run.json"
    common = [
        "--manifest",
        str(dataset / "manifest.jsonl"),
        "--frames",
        str(dataset / "frames.jsonl"),
        "--history-window",
        str(args.history_window),
        "--decay",
        str(args.decay),
        "--observation-ratio",
        str(args.observation_ratio),
        "--max-parallel-clips",
        str(args.max_parallel_clips),
    ]
    baseline = _demo_subprocess(
        [
            "run",
            *common,
            "--workdir",
            str(baseline_workdir),
            "--reset",
            "--report",
            str(baseline_report_path),
        ]
    )
    if baseline.returncode != 0:
        _print_json(
            {
                "status": "failed",
                "phase": "baseline",
                "returncode": baseline.returncode,
                "stdout": baseline.stdout,
                "stderr": baseline.stderr,
            }
        )
        return 2
    killed = _demo_subprocess(
        [
            "run",
            *common,
            "--workdir",
            str(recovery_workdir),
            "--reset",
            "--kill-after-stage",
            str(args.kill_after_stage),
            "--report",
            str(killed_report_path),
        ]
    )
    if killed.returncode != 137:
        _print_json(
            {
                "status": "failed",
                "phase": "forced_termination",
                "returncode": killed.returncode,
                "stdout": killed.stdout,
                "stderr": killed.stderr,
            }
        )
        return 2
    partial_checkpoint = SnapshotStore(recovery_workdir).load()
    if partial_checkpoint is None or partial_checkpoint["version"] != args.kill_after_stage:
        _print_json(
            {
                "status": "failed",
                "phase": "partial_checkpoint",
                "committed_version": (
                    partial_checkpoint["version"] if partial_checkpoint else None
                ),
            }
        )
        return 2
    rollback_workdir = root / "rollback"
    shutil.copytree(recovery_workdir, rollback_workdir)
    rollback_store = SnapshotStore(rollback_workdir)
    old_checkpoint_bytes = rollback_store.snapshot_path.read_bytes()
    resume = _demo_subprocess(
        [
            "run",
            *common,
            "--workdir",
            str(recovery_workdir),
            "--report",
            str(recovery_report_path),
        ]
    )
    if resume.returncode != 0:
        _print_json(
            {
                "status": "failed",
                "phase": "resume",
                "returncode": resume.returncode,
                "stdout": resume.stdout,
                "stderr": resume.stderr,
            }
        )
        return 2
    baseline_checkpoint = SnapshotStore(baseline_workdir).load()
    recovered_checkpoint = SnapshotStore(recovery_workdir).load()
    if baseline_checkpoint is None or recovered_checkpoint is None:
        _print_json({"status": "failed", "phase": "load_checkpoints"})
        return 2
    baseline_final = (baseline_workdir / "final" / "result.json").read_bytes()
    recovered_final = (recovery_workdir / "final" / "result.json").read_bytes()
    comparison = compare_recovery(
        baseline_checkpoint,
        recovered_checkpoint,
        reference_final_bytes=baseline_final,
        recovered_final_bytes=recovered_final,
    )
    comparison["checkpoint_bytes_equal"] = (
        SnapshotStore(baseline_workdir).snapshot_path.read_bytes()
        == SnapshotStore(recovery_workdir).snapshot_path.read_bytes()
    )
    comparison["kill_report_absent"] = not killed_report_path.exists()

    faults_dir = root / "faults"
    fault_report_path = faults_dir / "duplicate-clip.json"
    anomaly = _demo_subprocess(
        [
            "run",
            "--manifest",
            str(dataset / "manifest.jsonl"),
            "--frames",
            str(dataset / "frames.jsonl"),
            "--workdir",
            str(faults_dir / "run"),
            "--reset",
            "--inject-fault",
            "duplicate_clip_id",
            "--report",
            str(fault_report_path),
        ]
    )
    if anomaly.returncode != 2:
        _print_json(
            {
                "status": "failed",
                "phase": "anomaly_injection",
                "returncode": anomaly.returncode,
                "stdout": anomaly.stdout,
                "stderr": anomaly.stderr,
            }
        )
        return 2
    anomaly_report = _load_report(fault_report_path)

    corrupt_workdir = root / "corrupt"
    shutil.copytree(baseline_workdir, corrupt_workdir)
    corrupt_checkpoint = corrupt_workdir / "snapshots" / "checkpoint.json"
    corrupt_checkpoint.write_bytes(b"{not valid json\n")
    corrupt_report_path = root / "faults" / "corrupt-snapshot.json"
    corrupt_run = _demo_subprocess(
        [
            "verify",
            "--workdir",
            str(corrupt_workdir),
            "--report",
            str(corrupt_report_path),
        ]
    )
    if corrupt_run.returncode != 2:
        _print_json(
            {
                "status": "failed",
                "phase": "corrupt_snapshot",
                "returncode": corrupt_run.returncode,
                "stdout": corrupt_run.stdout,
                "stderr": corrupt_run.stderr,
            }
        )
        return 2
    corrupt_report = _load_report(corrupt_report_path)

    rollback_resume_report_path = root / "faults" / "rollback-resume.json"
    rollback_resume = _demo_subprocess(
        [
            "run",
            *common,
            "--workdir",
            str(rollback_workdir),
            "--report",
            str(rollback_resume_report_path),
        ]
    )
    if rollback_resume.returncode != 0:
        _print_json(
            {
                "status": "failed",
                "phase": "rollback_resume",
                "returncode": rollback_resume.returncode,
                "stdout": rollback_resume.stdout,
                "stderr": rollback_resume.stderr,
            }
        )
        return 2
    rollback_store.snapshot_path.write_bytes(old_checkpoint_bytes)
    rollback_report_path = root / "faults" / "rollback.json"
    rollback_check = _demo_subprocess(
        [
            "verify",
            "--workdir",
            str(rollback_workdir),
            "--report",
            str(rollback_report_path),
        ]
    )
    if rollback_check.returncode != 2:
        _print_json(
            {
                "status": "failed",
                "phase": "rollback_detection",
                "returncode": rollback_check.returncode,
                "stdout": rollback_check.stdout,
                "stderr": rollback_check.stderr,
            }
        )
        return 2
    rollback_report = _load_report(rollback_report_path)

    replace_workdir = root / "replace"
    shutil.copytree(baseline_workdir, replace_workdir)
    replace_store = SnapshotStore(replace_workdir)
    replacement_source = replace_store.load()
    if replacement_source is None:
        _print_json({"status": "failed", "phase": "replacement_source"})
        return 2
    old_checksum = replacement_source["snapshot_checksum"]
    replacement_source["replacement_probe"] = "whole_file"
    replacement_candidate = resign_snapshot(replacement_source)
    replace_store.replace_snapshot(
        replacement_candidate, expected_version=replacement_candidate["version"]
    )
    replaced = replace_store.load()
    if replaced is None:
        _print_json({"status": "failed", "phase": "replacement_load"})
        return 2
    stage_payloads_preserved = all(
        base["content_checksum"] == after["content_checksum"]
        for base, after in zip(
            baseline_checkpoint["stages"], replaced["stages"]
        )
    )
    replacement_probe = {
        "same_version": True,
        "old_checksum": old_checksum,
        "new_checksum": replaced["snapshot_checksum"],
        "stage_payloads_preserved": stage_payloads_preserved,
        "atomic_whole_file_replace": True,
        "ledger_updated": True,
    }
    verify_checkpoint(replaced)
    demo_report: dict[str, Any] = {
        "schema_version": 1,
        "command": "demo",
        "status": "passed",
        "ok": True,
        "dataset": {
            "path": "dataset",
            "seed": args.seed,
            "clip_count": args.clips,
        },
        "baseline": {
            "status": _load_report(baseline_report_path)["status"],
            "final_result_sha256": sha256_bytes(baseline_final),
            "checkpoint_sha256": sha256_bytes(
                SnapshotStore(baseline_workdir).snapshot_path.read_bytes()
            ),
            "stage_versions": baseline_checkpoint["version_history"],
        },
        "forced_termination": {
            "stage": args.kill_after_stage,
            "exit_code": killed.returncode,
            "committed_version": partial_checkpoint["version"],
            "committed_stage": partial_checkpoint["committed_stage"],
            "exit_report_written": killed_report_path.exists(),
        },
        "recovery": {
            "status": _load_report(recovery_report_path)["status"],
            "reused": _load_report(recovery_report_path)["reused"],
            "recomputed": _load_report(recovery_report_path)["recomputed"],
            "final_result_sha256": sha256_bytes(recovered_final),
            "checkpoint_sha256": sha256_bytes(
                SnapshotStore(recovery_workdir).snapshot_path.read_bytes()
            ),
        },
        "recovery_comparison": comparison,
        "anomaly_injection": {
            "kind": anomaly_report["fault_injection"]["kind"],
            "business_code": anomaly_report["business_code"],
            "field": anomaly_report.get("field"),
            "clip_id": anomaly_report.get("clip_id"),
        },
        "snapshot_corruption": {
            "business_code": corrupt_report["business_code"],
            "stage": corrupt_report["business_error"].get("stage"),
            "version": corrupt_report["business_error"].get("version"),
        },
        "snapshot_rollback": {
            "business_code": rollback_report["business_code"],
            "stage": rollback_report["business_error"].get("stage"),
            "version": rollback_report["business_error"].get("version"),
            "candidate_version": partial_checkpoint["version"],
        },
        "whole_snapshot_replacement": replacement_probe,
        "degradations": _load_report(baseline_report_path).get("degradations", []),
    }
    demo_report["report_path"] = str(root / "report.json")
    _write_report(root / "report.json", demo_report)
    _print_json(
        {
            "status": "passed",
            "ok": True,
            "report": str(root / "report.json"),
            "baseline_final_sha256": demo_report["baseline"][
                "final_result_sha256"
            ],
            "recovery_equal": True,
        }
    )
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="motion_pipeline",
        description="Deterministic local video-prediction pipeline",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    generate_parser = subparsers.add_parser("generate", help="generate local samples")
    generate_parser.add_argument("--out", required=True)
    generate_parser.add_argument("--seed", type=int, default=20261123)
    generate_parser.add_argument("--clips", type=int, default=6)
    generate_parser.set_defaults(handler=command_generate)

    run_parser = subparsers.add_parser("run", help="run or resume the pipeline")
    run_parser.add_argument("--manifest", required=True)
    run_parser.add_argument("--frames", required=True)
    run_parser.add_argument("--workdir", required=True)
    run_parser.add_argument("--report")
    run_parser.add_argument("--reset", action="store_true")
    run_parser.add_argument("--kill-after-stage", type=int, choices=range(1, 5))
    run_parser.add_argument(
        "--inject-fault",
        default="none",
        choices=(
            "none",
            "duplicate_clip_id",
            "frame_count_mismatch",
            "unknown_temporal_policy",
            "frame_interval_nonpositive",
            "motion_mode_unknown",
            "resource_limit",
            "window_invalid",
            "decay_invalid",
        ),
    )
    run_parser.add_argument("--history-window", type=int, default=6)
    run_parser.add_argument("--decay", type=float, default=0.85)
    run_parser.add_argument("--observation-ratio", type=float, default=0.62)
    run_parser.add_argument("--max-frames-per-clip", type=int, default=96)
    run_parser.add_argument(
        "--max-resident-history-windows", type=int, default=8
    )
    run_parser.add_argument("--max-parallel-clips", type=int, default=4)
    run_parser.add_argument(
        "--max-history-window-length", type=int, default=64
    )
    run_parser.add_argument(
        "--resource-policy", choices=("degrade", "error"), default="degrade"
    )
    run_parser.add_argument("--hit-distance-px", type=float, default=2.5)
    run_parser.set_defaults(handler=command_run)

    verify_parser = subparsers.add_parser("verify", help="verify committed results")
    verify_parser.add_argument("--workdir", required=True)
    verify_parser.add_argument("--report")
    verify_parser.set_defaults(handler=command_verify)

    compare_parser = subparsers.add_parser(
        "compare", help="compare uninterrupted and recovered runs"
    )
    compare_parser.add_argument("--baseline-workdir", required=True)
    compare_parser.add_argument("--recovery-workdir", required=True)
    compare_parser.add_argument("--report")
    compare_parser.set_defaults(handler=command_compare)

    demo_parser = subparsers.add_parser(
        "demo", help="run baseline, forced-kill recovery, and fault probes"
    )
    demo_parser.add_argument("--out", default="demo-output")
    demo_parser.add_argument("--seed", type=int, default=20261123)
    demo_parser.add_argument("--clips", type=int, default=6)
    demo_parser.add_argument("--history-window", type=int, default=6)
    demo_parser.add_argument("--decay", type=float, default=0.85)
    demo_parser.add_argument("--observation-ratio", type=float, default=0.62)
    demo_parser.add_argument("--max-parallel-clips", type=int, default=4)
    demo_parser.add_argument("--kill-after-stage", type=int, choices=(1, 2, 3), default=2)
    demo_parser.set_defaults(handler=command_demo)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return int(args.handler(args))
