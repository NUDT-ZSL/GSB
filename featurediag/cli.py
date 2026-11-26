"""Command-line interface and deterministic end-to-end demonstration."""

from __future__ import annotations

import argparse
import fcntl
import json
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

from .canonical import atomic_write_json, atomic_write_jsonl
from .cache import CacheIndex, build_invalidation_plan
from .errors import BusinessError, SimulatedCrash
from .generator import MASTER_SEED, init_data
from .models import (
    Batch,
    RunConfig,
    Task,
    load_batches,
    load_tasks,
    parse_task,
    validate_task_against_registry,
)
from .pipeline import (
    extract_baseline_items,
    run_fingerprint,
    run_pipeline,
    verify_run,
)
from .snapshot import (
    Snapshot,
    SnapshotCommitStore,
    migrate_snapshot,
    read_snapshot,
)


DEMO_RUNS = ["demo-baseline", "demo-repro", "demo-recovery", "demo-revision"]


def _print_json(value: Any, *, stream: Any = None) -> None:
    if stream is None:
        stream = sys.stdout
    stream.write(json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + "\n")


def _emit_error(error: BusinessError, *, stream: Any = None) -> None:
    if stream is None:
        stream = sys.stderr
    stream.write(json.dumps(error.to_dict(), ensure_ascii=False, sort_keys=True, allow_nan=False) + "\n")


def _load_context(
    root: Path,
    *,
    tasks_path: Path,
    batches_path: Path,
    snapshot_path: Path,
    config_path: Path,
) -> tuple[RunConfig, dict[str, Task], list[Batch], Snapshot]:
    config = RunConfig.load(config_path)
    tasks = load_tasks(tasks_path, default_tolerance=config.tolerance)
    batches = load_batches(batches_path, tasks)
    snapshot = read_snapshot(snapshot_path)
    return config, tasks, batches, snapshot


def _commit_source_snapshot(
    root: Path, snapshot: Snapshot, *, hold_lock_ms: int = 0
) -> dict[str, Any]:
    store = SnapshotCommitStore(root / "artifacts" / "snapshots")
    return store.commit(snapshot, hold_lock_ms=hold_lock_ms)


def _run_command(args: argparse.Namespace, root: Path) -> dict[str, Any]:
    config, tasks, batches, source_snapshot = _load_context(
        root,
        tasks_path=root / args.tasks,
        batches_path=root / args.batches,
        snapshot_path=root / args.snapshot,
        config_path=root / args.config,
    )
    registry_path = root / args.registry
    validate_task_against_registry(tasks, registry_path, update=False)
    commit_result = _commit_source_snapshot(root, source_snapshot)
    active_snapshot = SnapshotCommitStore(root / "artifacts" / "snapshots").read_active()
    if active_snapshot is None:
        raise BusinessError("E_FORMAT_CORRUPT", "active snapshot was not committed")
    result = run_pipeline(
        root=root,
        run_id=args.run_id,
        tasks=tasks,
        batches=batches,
        snapshot=active_snapshot,
        config=config,
        fresh=args.fresh,
        crash_after_stage=args.crash_after_stage,
        use_cache=not args.no_cache,
    )
    validate_task_against_registry(tasks, registry_path, update=True)
    result["snapshot_commit"] = commit_result
    summary = {
        key: result[key]
        for key in (
            "code",
            "exit_code",
            "run_id",
            "run_dir",
            "ledger_path",
            "ledger_checksum",
            "ledger_entry_count",
            "failed_entry_count",
            "checkpoints",
            "cache_hits",
            "cache_recomputed",
            "recovered_from_committed_stages",
            "discarded_uncommitted_paths",
            "errors",
            "snapshot_commit",
        )
    }
    return summary


def _validate_command(args: argparse.Namespace, root: Path) -> dict[str, Any]:
    config, tasks, batches, snapshot = _load_context(
        root,
        tasks_path=root / args.tasks,
        batches_path=root / args.batches,
        snapshot_path=root / args.snapshot,
        config_path=root / args.config,
    )
    registry_path = root / args.registry
    registry = validate_task_against_registry(
        tasks, registry_path, update=args.update_registry
    )
    return {
        "code": "OK",
        "task_count": len(tasks),
        "batch_count": len(batches),
        "batch_ids": [batch.batch_id for batch in batches],
        "snapshot": {
            "version": snapshot.snapshot_version,
            "checksum": snapshot.checksum(),
            "format_version": snapshot.format_version,
            "active_features": sorted(snapshot.active_feature_ids),
            "invalid_features": sorted(snapshot.invalid_feature_ids),
        },
        "run_fingerprint": run_fingerprint(tasks, batches, snapshot, config),
        "registry": registry,
    }


def _invalidate_command(args: argparse.Namespace, root: Path) -> dict[str, Any]:
    config = RunConfig.load(root / args.config)
    old_tasks = load_tasks(root / args.tasks, default_tolerance=config.tolerance)
    new_tasks = load_tasks(root / args.new_tasks, default_tolerance=config.tolerance)
    batches = load_batches(root / args.batches, new_tasks)
    old_snapshot = read_snapshot(root / args.snapshot)
    new_snapshot = read_snapshot(root / args.new_snapshot)
    batch_ids = [batch.batch_id for batch in batches]
    baseline = extract_baseline_items(root, args.baseline_run, batch_ids)
    cache_index = CacheIndex(root / "artifacts" / "cache" / "index.json").load()
    plan = build_invalidation_plan(
        old_tasks=old_tasks,
        new_tasks=new_tasks,
        batches=batches,
        old_snapshot=old_snapshot,
        new_snapshot=new_snapshot,
        baseline_items=baseline,
        cache_index=cache_index,
    )
    output = root / args.output
    atomic_write_json(output, plan)
    plan["output_path"] = str(output)
    return plan


def _demo_lock_holder(root: Path, snapshot_path: str) -> None:
    command = [
        sys.executable,
        str(root / "benchmark.py"),
        "commit-snapshot",
        snapshot_path,
        "--hold-lock-ms",
        "1200",
    ]
    return subprocess.Popen(
        command,
        cwd=root,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )


def _wait_for_lock_holder(lock_path: Path, timeout: float = 3.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if lock_path.exists():
            with lock_path.open("a+b") as handle:
                try:
                    fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError:
                    return True
                finally:
                    try:
                        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
                    except OSError:
                        pass
        time.sleep(0.02)
    return False


def _demo_error_codes(root: Path, tasks: dict[str, Task], batches: list[Batch]) -> list[dict[str, Any]]:
    error_dir = root / "artifacts" / "demo" / "error-codes"
    error_dir.mkdir(parents=True, exist_ok=True)
    observations: list[dict[str, Any]] = []

    duplicate_tasks = [tasks["task-alpha"].raw, tasks["task-beta"].raw, tasks["task-alpha"].raw]
    atomic_write_jsonl(error_dir / "duplicate_tasks.jsonl", duplicate_tasks)
    try:
        load_tasks(error_dir / "duplicate_tasks.jsonl")
    except BusinessError as exc:
        observations.append({"code": exc.code, "context": exc.context})

    duplicate_batches = [batch.raw for batch in batches] + [batches[0].raw]
    atomic_write_jsonl(error_dir / "duplicate_batches.jsonl", duplicate_batches)
    try:
        load_batches(error_dir / "duplicate_batches.jsonl", tasks)
    except BusinessError as exc:
        observations.append({"code": exc.code, "context": exc.context})

    invalid_raw = dict(tasks["task-alpha"].raw)
    invalid_raw["quant_scale"] = 0.0
    try:
        parse_task(invalid_raw, source="demo", default_tolerance=RunConfig.load(root / "data" / "config.json").tolerance)
    except BusinessError as exc:
        observations.append({"code": exc.code, "context": exc.context})

    registry_path = error_dir / "rollback_registry.json"
    atomic_write_json(
        registry_path,
        {"task-alpha": {"revision": 1, "logical_clock_ms": 2000}},
    )
    rollback_raw = dict(tasks["task-alpha"].raw)
    rollback_raw["revision"] = 0
    rollback_task = parse_task(
        rollback_raw,
        source="demo",
        default_tolerance=RunConfig.load(root / "data" / "config.json").tolerance,
    )
    try:
        validate_task_against_registry({"task-alpha": rollback_task}, registry_path)
    except BusinessError as exc:
        observations.append({"code": exc.code, "context": exc.context})

    clock_raw = dict(tasks["task-alpha"].raw)
    clock_raw["logical_clock_ms"] = 1999
    clock_task = parse_task(
        clock_raw,
        source="demo",
        default_tolerance=RunConfig.load(root / "data" / "config.json").tolerance,
    )
    try:
        validate_task_against_registry({"task-alpha": clock_task}, registry_path)
    except BusinessError as exc:
        observations.append({"code": exc.code, "context": exc.context})

    return observations


def _demo_command(root: Path, args: argparse.Namespace) -> dict[str, Any]:
    artifacts = root / "artifacts"
    artifacts.mkdir(parents=True, exist_ok=True)
    for run_id in DEMO_RUNS:
        shutil.rmtree(artifacts / "runs" / run_id, ignore_errors=True)
    shutil.rmtree(artifacts / "cache", ignore_errors=True)
    shutil.rmtree(artifacts / "snapshots", ignore_errors=True)
    shutil.rmtree(artifacts / "demo", ignore_errors=True)
    (artifacts / "state").mkdir(parents=True, exist_ok=True)
    (artifacts / "state" / "task_registry.json").unlink(missing_ok=True)
    init_data(root, force=True)
    config, tasks, batches, snapshot = _load_context(
        root,
        tasks_path=root / "data" / "tasks.jsonl",
        batches_path=root / "data" / "batches.jsonl",
        snapshot_path=root / "data" / "spec_v2",
        config_path=root / "data" / "config.json",
    )
    registry_path = artifacts / "state" / "task_registry.json"
    first_commit = _commit_source_snapshot(root, snapshot)
    active_snapshot = SnapshotCommitStore(artifacts / "snapshots").read_active()
    if active_snapshot is None:
        raise BusinessError("E_FORMAT_CORRUPT", "demo active snapshot is missing after commit")
    baseline = run_pipeline(
        root=root,
        run_id="demo-baseline",
        tasks=tasks,
        batches=batches,
        snapshot=active_snapshot,
        config=config,
        fresh=True,
        use_cache=True,
    )
    validate_task_against_registry(tasks, registry_path, update=True)

    repro = run_pipeline(
        root=root,
        run_id="demo-repro",
        tasks=tasks,
        batches=batches,
        snapshot=active_snapshot,
        config=config,
        fresh=True,
        use_cache=False,
    )
    reproducibility = {
        "ledger_checksum_baseline": baseline["ledger_checksum"],
        "ledger_checksum_repro": repro["ledger_checksum"],
        "ledger_byte_equivalent": baseline["ledger_checksum"] == repro["ledger_checksum"],
        "entry_count_equal": baseline["ledger_entry_count"] == repro["ledger_entry_count"],
        "baseline_cache_hits": len(baseline["cache_hits"]),
    }

    try:
        run_pipeline(
            root=root,
            run_id="demo-recovery",
            tasks=tasks,
            batches=batches,
            snapshot=active_snapshot,
            config=config,
            fresh=True,
            crash_after_stage=2,
            use_cache=False,
        )
    except SimulatedCrash:
        pass
    stale = artifacts / "runs" / "demo-recovery" / ".stage_03.v4.tmp"
    stale.mkdir(parents=True, exist_ok=True)
    atomic_write_json(stale / "orphan.json", {"kind": "uncommitted-intermediate"})
    orphan = artifacts / "runs" / "demo-recovery" / "stage_03.v999"
    orphan.mkdir(parents=True, exist_ok=True)
    atomic_write_json(orphan / "orphan.json", {"kind": "unreferenced-stage"})
    recovered = run_pipeline(
        root=root,
        run_id="demo-recovery",
        tasks=tasks,
        batches=batches,
        snapshot=active_snapshot,
        config=config,
        use_cache=False,
    )
    verified = verify_run(root, "demo-recovery", tasks, batches)
    recovery = {
        "ledger_checksum": recovered["ledger_checksum"],
        "matches_baseline": recovered["ledger_checksum"] == baseline["ledger_checksum"],
        "recovered_from": recovered["recovered_from_committed_stages"],
        "discarded": recovered["discarded_uncommitted_paths"],
        "verification": verified,
    }

    migration_dir = artifacts / "demo" / "migrations"
    migration_dir.mkdir(parents=True, exist_ok=True)
    v1_to_v2 = migrate_snapshot(
        root / "data" / "spec_v1.json",
        migration_dir / "v1-to-v2",
        report_path=migration_dir / "v1-to-v2-report.json",
    )
    v2_to_v1 = migrate_snapshot(
        root / "data" / "spec_v2",
        migration_dir / "v2-to-v1.json",
        report_path=migration_dir / "v2-to-v1-report.json",
    )
    roundtrip = migrate_snapshot(
        migration_dir / "v2-to-v1.json",
        migration_dir / "roundtrip-v2",
        report_path=migration_dir / "roundtrip-report.json",
    )
    migration = {
        "v1_to_v2": {
            "active_set_preserved": v1_to_v2["active_features_before"] == v1_to_v2["active_features_after"],
            "invalid_set_preserved": v1_to_v2["invalid_features_before"] == v1_to_v2["invalid_features_after"],
            "millisecond_boundaries_preserved": v1_to_v2["millisecond_boundaries_preserved"],
        },
        "v2_to_v1": {
            "active_set_preserved": v2_to_v1["active_features_before"] == v2_to_v1["active_features_after"],
            "invalid_set_preserved": v2_to_v1["invalid_features_before"] == v2_to_v1["invalid_features_after"],
            "millisecond_boundaries_preserved": v2_to_v1["millisecond_boundaries_preserved"],
            "dropped_tombstone_count": len(v2_to_v1["dropped_tombstones"]),
        },
        "roundtrip": {
            "active_set_preserved": roundtrip["active_features_before"] == roundtrip["active_features_after"],
            "invalid_set_preserved": roundtrip["invalid_features_before"] == roundtrip["invalid_features_after"],
            "millisecond_boundaries_preserved": roundtrip["millisecond_boundaries_preserved"],
        },
    }

    new_tasks = load_tasks(root / "data" / "tasks_next.jsonl", default_tolerance=config.tolerance)
    new_snapshot = read_snapshot(root / "data" / "spec_v2_next")
    baseline_items = extract_baseline_items(
        root, "demo-baseline", [batch.batch_id for batch in batches]
    )
    cache_index = CacheIndex(artifacts / "cache" / "index.json").load()
    invalidation = build_invalidation_plan(
        old_tasks=tasks,
        new_tasks=new_tasks,
        batches=batches,
        old_snapshot=read_snapshot(root / "data" / "spec_v2"),
        new_snapshot=new_snapshot,
        baseline_items=baseline_items,
        cache_index=cache_index,
    )
    atomic_write_json(artifacts / "demo" / "invalidation_plan.json", invalidation)

    store = SnapshotCommitStore(artifacts / "snapshots")
    holder = _demo_lock_holder(root, "data/spec_v2")
    lock_path = artifacts / "snapshots" / "active_snapshot.lock"
    busy_observed = False
    if _wait_for_lock_holder(lock_path):
        try:
            store.commit(new_snapshot)
        except BusinessError as exc:
            if exc.code != "E_STAGE_BUSY":
                raise
            busy_observed = True
    holder.communicate(timeout=5)
    replacement = store.commit(new_snapshot)
    active_after = store.read_active()
    if active_after is None or active_after.checksum() != new_snapshot.checksum():
        raise BusinessError("E_FORMAT_CORRUPT", "active snapshot replacement failed")
    snapshot_rollback_observed = False
    try:
        store.commit(read_snapshot(root / "data" / "spec_v1.json"))
    except BusinessError as exc:
        if exc.code != "E_SNAPSHOT_ROLLBACK":
            raise
        snapshot_rollback_observed = True
    snapshot_control = {
        "initial_commit_changed": first_commit["changed"],
        "busy_observed": busy_observed,
        "replacement_mode": replacement["replacement_mode"],
        "replacement_changed": replacement["changed"],
        "active_checksum_equals_incoming": active_after.checksum() == new_snapshot.checksum(),
        "snapshot_rollback_observed": snapshot_rollback_observed,
    }

    validate_task_against_registry(new_tasks, registry_path, update=False)
    revision = run_pipeline(
        root=root,
        run_id="demo-revision",
        tasks=new_tasks,
        batches=batches,
        snapshot=active_after,
        config=config,
        fresh=True,
        use_cache=True,
    )
    validate_task_against_registry(new_tasks, registry_path, update=True)
    atomic_write_json(
        registry_path,
        {
            task_id: {
                "revision": task.revision,
                "logical_clock_ms": task.logical_clock_ms,
                "feature_dim": task.feature_dim,
                "feature_ids": list(task.feature_ids),
            }
            for task_id, task in sorted(tasks.items())
        },
    )
    revision_cache_hits = revision["cache_hits"]
    revision_recomputed = revision["cache_recomputed"]
    invalidation_checks = {
        "affected_tasks": invalidation["affected_tasks"],
        "affected_features": invalidation["affected_features"],
        "reuse_count": len(invalidation["reuse_manifest"]),
        "recompute_count": len(invalidation["recompute_manifest"]),
        "alpha_only_affected": set(invalidation["affected_tasks"]) == {"task-alpha"},
        "beta_fully_reusable": all(
            decision["decision"] == "reuse"
            for task in invalidation["tasks"]
            if task["task_id"] == "task-beta"
            for batch in task["batches"]
            for decision in batch["stages"].values()
        ),
        "actual_cache_hits": revision_cache_hits,
        "actual_recomputed": revision_recomputed,
        "actual_recompute_alpha_suffix_only": {
            item["task_id"] for item in revision_recomputed
        }
        == {"task-alpha"},
    }

    error_codes = _demo_error_codes(root, tasks, batches)
    all_checks = {
        "reproducible_ledger": reproducibility["ledger_byte_equivalent"],
        "recovery_verified": recovery["matches_baseline"] and recovery["verification"]["code"] == "OK",
        "recovery_discarded_orphan_temps": len(recovery["discarded"]) == 2,
        "migration_bounds_and_invalid_set": all(
            item["active_set_preserved"]
            and item["invalid_set_preserved"]
            and item["millisecond_boundaries_preserved"]
            for item in (migration["v1_to_v2"], migration["v2_to_v1"], migration["roundtrip"])
        ),
        "invalidation_targeted_suffix": invalidation_checks["alpha_only_affected"]
        and invalidation_checks["beta_fully_reusable"]
        and invalidation_checks["actual_recompute_alpha_suffix_only"],
        "exclusive_snapshot_commit": busy_observed,
        "whole_snapshot_replacement": snapshot_control["replacement_changed"]
        and snapshot_control["active_checksum_equals_incoming"],
        "rollback_detected": snapshot_rollback_observed,
        "core_error_codes_observed": {
            item["code"] for item in error_codes
        }
        >= {"E_DUP_TASK", "E_BAD_BATCH", "E_PARAM_INVALID", "E_REVISION_ROLLBACK", "E_CLOCK_ROLLBACK"},
    }
    report = {
        "schema": "featurediag.verification-report.v1",
        "master_seed": MASTER_SEED,
        "baseline": {
            "run_id": "demo-baseline",
            "code": baseline["code"],
            "ledger_checksum": baseline["ledger_checksum"],
            "entry_count": baseline["ledger_entry_count"],
            "failed_entry_count": baseline["failed_entry_count"],
            "checkpoints": baseline["checkpoints"],
        },
        "reproducibility": reproducibility,
        "recovery": recovery,
        "snapshot_migration": migration,
        "invalidation": invalidation_checks,
        "snapshot_control": snapshot_control,
        "error_codes_observed": error_codes,
        "checks": all_checks,
        "all_checks_passed": all(all_checks.values()),
    }
    atomic_write_json(artifacts / "demo" / "verification_report.json", report)
    if not report["all_checks_passed"]:
        raise BusinessError(
            "E_INTERNAL",
            "one or more demonstration checks failed",
            context={"checks": all_checks, "report": str(artifacts / "demo" / "verification_report.json")},
        )
    return {
        "code": "OK",
        "report_path": str(artifacts / "demo" / "verification_report.json"),
        "all_checks_passed": True,
        "baseline_ledger_checksum": baseline["ledger_checksum"],
        "recovery_ledger_checksum": recovery["ledger_checksum"],
        "invalidation": invalidation_checks,
        "snapshot_control": snapshot_control,
        "error_codes": [item["code"] for item in error_codes],
        "checks": all_checks,
    }


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="benchmark.py",
        description="Deterministic feature numerical-error diagnostic application",
    )
    parser.add_argument("--root", default=".", help="workspace root (default: current directory)")
    subparsers = parser.add_subparsers(dest="command")

    init_parser = subparsers.add_parser("init", help="generate fixed-seed input fixtures")
    init_parser.add_argument("--force", action="store_true")

    validate_parser = subparsers.add_parser("validate", help="validate inputs and configuration")
    validate_parser.add_argument("--tasks", default="data/tasks.jsonl")
    validate_parser.add_argument("--batches", default="data/batches.jsonl")
    validate_parser.add_argument("--snapshot", default="data/spec_v2")
    validate_parser.add_argument("--config", default="data/config.json")
    validate_parser.add_argument("--registry", default="artifacts/state/task_registry.json")
    validate_parser.add_argument("--update-registry", action="store_true")

    run_parser = subparsers.add_parser("run", help="run four-stage diagnostics")
    run_parser.add_argument("--run-id", default="diagnostic")
    run_parser.add_argument("--tasks", default="data/tasks.jsonl")
    run_parser.add_argument("--batches", default="data/batches.jsonl")
    run_parser.add_argument("--snapshot", default="data/spec_v2")
    run_parser.add_argument("--config", default="data/config.json")
    run_parser.add_argument("--registry", default="artifacts/state/task_registry.json")
    run_parser.add_argument("--fresh", action="store_true")
    run_parser.add_argument("--no-cache", action="store_true")
    run_parser.add_argument(
        "--crash-after-stage",
        type=int,
        choices=[0, 1, 2, 3],
        default=None,
        help="internal fault injection after a committed stage",
    )

    migrate_parser = subparsers.add_parser("migrate", help="migrate v1/v2 snapshots")
    migrate_parser.add_argument("--from", dest="source", required=True)
    migrate_parser.add_argument("--to", dest="target", required=True)
    migrate_parser.add_argument("--report", default=None)
    migrate_parser.add_argument("--epoch-ms", type=int, default=0)

    commit_parser = subparsers.add_parser("commit-snapshot", help="atomically commit a snapshot")
    commit_parser.add_argument("snapshot")
    commit_parser.add_argument("--hold-lock-ms", type=int, default=0)

    invalidate_parser = subparsers.add_parser("invalidate", help="build targeted invalidation plan")
    invalidate_parser.add_argument("--baseline-run", default="diagnostic")
    invalidate_parser.add_argument("--tasks", default="data/tasks.jsonl")
    invalidate_parser.add_argument("--new-tasks", default="data/tasks_next.jsonl")
    invalidate_parser.add_argument("--batches", default="data/batches.jsonl")
    invalidate_parser.add_argument("--snapshot", default="data/spec_v2")
    invalidate_parser.add_argument("--new-snapshot", default="data/spec_v2_next")
    invalidate_parser.add_argument("--config", default="data/config.json")
    invalidate_parser.add_argument("--output", default="artifacts/invalidation_plan.json")

    verify_parser = subparsers.add_parser("verify", help="verify checkpoints, artifacts, and ledger")
    verify_parser.add_argument("--run-id", default="diagnostic")
    verify_parser.add_argument("--tasks", default="data/tasks.jsonl")
    verify_parser.add_argument("--batches", default="data/batches.jsonl")
    verify_parser.add_argument("--config", default="data/config.json")

    demo_parser = subparsers.add_parser("demo", help="run the full deterministic acceptance demonstration")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    root = Path(args.root).resolve()
    command = args.command or "demo"
    if args.command is None:
        args = parser.parse_args(["--root", str(root), "demo"])
        command = "demo"
    try:
        if command == "init":
            result = init_data(root, force=args.force)
        elif command == "validate":
            result = _validate_command(args, root)
        elif command == "run":
            result = _run_command(args, root)
        elif command == "migrate":
            report_path = root / args.report if args.report else None
            result = migrate_snapshot(
                root / args.source,
                root / args.target,
                report_path=report_path,
                epoch_ms=args.epoch_ms,
            )
        elif command == "commit-snapshot":
            snapshot = read_snapshot(root / args.snapshot)
            result = SnapshotCommitStore(root / "artifacts" / "snapshots").commit(
                snapshot, hold_lock_ms=args.hold_lock_ms
            )
        elif command == "invalidate":
            result = _invalidate_command(args, root)
        elif command == "verify":
            config = RunConfig.load(root / args.config)
            tasks = load_tasks(root / args.tasks, default_tolerance=config.tolerance)
            batches = load_batches(root / args.batches, tasks)
            result = verify_run(root, args.run_id, tasks, batches)
        elif command == "demo":
            result = _demo_command(root, args)
        else:
            parser.error(f"unknown command {command}")
            return 2
        _print_json(result)
        return int(result.get("exit_code", 0))
    except SimulatedCrash as exc:
        error = BusinessError(
            "E_SIMULATED_CRASH",
            str(exc),
            context={"note": "committed checkpoints remain; rerun without the crash flag"},
        )
        _emit_error(error)
        return error.exit_code
    except BusinessError as exc:
        _emit_error(exc)
        return exc.exit_code
    except Exception as exc:
        error = BusinessError(
            "E_INTERNAL",
            f"internal error: {type(exc).__name__}: {exc}",
        )
        _emit_error(error)
        return error.exit_code
