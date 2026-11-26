"""Command line entry points for generation, runs, migration and verification."""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any

from .canonical import (
    atomic_write_bytes,
    atomic_write_json,
    canonical_json,
    sha256_file,
)
from .checkpoint import StageStateMachine, checkpoint_error_replay
from .config import PipelineConfig
from .errors import DriftError, E_STAGE_BUSY, E_VERSION_UNSUPPORTED
from .ledger import read_ledger, replay_ledger, write_ledger
from .pipeline import compare_snapshot_runs, run_pipeline
from .samplegen import generate_dataset
from .snapshots import (
    migrate_v1_to_v2,
    migrate_v2_to_v1,
    read_snapshot,
    snapshot_error_replay,
)
from .thresholds import compare_threshold


def _default_data_dir() -> Path:
    return Path(__file__).resolve().parents[1] / "data"


def _write_run_artifacts(result: dict[str, Any], output_dir: Path) -> None:
    output_dir.mkdir(parents=True, exist_ok=True)
    atomic_write_json(output_dir / "result.json", result)
    atomic_write_json(output_dir / "aligned.json", result["alignment"])
    atomic_write_json(
        output_dir / "windows.json",
        {
            "schema": "window-summary-v1",
            "windows": [
                {
                    "window_id": window["window_id"],
                    "start_ms": window["start_ms"],
                    "end_ms": window["end_ms"],
                    "statistics": window["statistics"],
                    "decision": window["decision"],
                    "digest": window["digest"],
                }
                for window in result["windows"]
            ],
        },
    )
    write_ledger(output_dir / "ledger.jsonl", result["ledger_entries"])
    atomic_write_json(output_dir / "summary.json", result["summary"])


def _run_command(args: argparse.Namespace) -> int:
    config = PipelineConfig.load(args.config)
    changes = None
    if args.changes:
        with Path(args.changes).open("r", encoding="utf-8") as handle:
            changes = json.load(handle)
    checkpoint = Path(args.checkpoint) if args.checkpoint else None
    lock_path = Path(args.lock) if args.lock else None
    result = run_pipeline(
        config,
        args.observations,
        args.snapshot,
        cache_dir=args.cache_dir,
        use_cache=not args.no_cache,
        changes=changes,
        checkpoint_path=checkpoint,
        lock_path=lock_path,
    )
    _write_run_artifacts(result, Path(args.out))
    if checkpoint is not None:
        state = StageStateMachine(checkpoint, lock_path=lock_path)
        state.commit_stage(
            "exit_report",
            records=(f"run:{result['comparison_digest']}",),
            state="exit_report",
            exit_requested=True,
            pending_exit="normal_completion",
        )
        state.acknowledge_exit()
    print(json.dumps(result["summary"], ensure_ascii=False, sort_keys=True))
    print(f"comparison_digest={result['comparison_digest']}")
    print(f"artifacts={args.out}")
    return 0


def _lease_probe(lock_path: Path) -> dict[str, Any]:
    checkpoint = lock_path.parent / "busy-probe-checkpoint.json"
    script = (
        "import time\n"
        "from telemetry_drift.locking import FileCommitLock\n"
        f"lock=FileCommitLock({str(lock_path)!r})\n"
        "lock.acquire()\n"
        "print('locked', flush=True)\n"
        "time.sleep(1.2)\n"
    )
    environment = dict(os.environ)
    root = str(Path(__file__).resolve().parents[1])
    environment["PYTHONPATH"] = (
        root
        + (
            os.pathsep + environment["PYTHONPATH"]
            if environment.get("PYTHONPATH")
            else ""
        )
    )
    process = subprocess.Popen(
        [sys.executable, "-c", script],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env=environment,
    )
    try:
        first_line = process.stdout.readline() if process.stdout else ""
        if first_line.strip() != "locked":
            stderr = process.stderr.read() if process.stderr else ""
            raise RuntimeError(f"lease probe failed: {stderr}")
        machine = StageStateMachine(checkpoint, lock_path=lock_path)
        try:
            machine.commit_stage("ingest", records=("busy-probe",))
        except DriftError as exc:
            return {
                "ok": exc.code == E_STAGE_BUSY,
                "code": exc.code,
                "details": exc.details,
            }
        return {"ok": False, "code": "NO_BUSY_ERROR"}
    finally:
        process.wait(timeout=5)


def _verify_command(args: argparse.Namespace) -> int:
    data = Path(args.data)
    output = Path(args.out)
    output.mkdir(parents=True, exist_ok=True)
    if args.regenerate:
        generate_dataset(data)
    run_root = Path(tempfile.mkdtemp(prefix="verify-", dir=output))
    config = PipelineConfig.load(data / "predictor_config.json")

    comparison = compare_snapshot_runs(
        config,
        data / "observations.jsonl",
        data / "predictor_v1.json",
        data / "predictor_v2.json",
        cache_dir=run_root / "comparison-cache",
    )
    first = run_pipeline(
        config,
        data / "observations.jsonl",
        data / "predictor_v1.json",
        cache_dir=run_root / "cache",
    )
    second = run_pipeline(
        config,
        data / "observations.jsonl",
        data / "predictor_v1.json",
        cache_dir=run_root / "cache",
    )
    revision = run_pipeline(
        config,
        data / "observations.jsonl",
        data / "predictor_v1.json",
        cache_dir=run_root / "cache",
        changes={
            "changed_variables": [1],
            "revisions": [
                {"variable": 1, "start_ms": 1800, "end_ms": 2200}
            ],
        },
    )
    replay = replay_ledger(first["ledger_entries"], config)

    upgrade = migrate_v1_to_v2(
        data / "predictor_v1.json",
        run_root / "upgraded-v2.json",
        epoch_ms=0,
        lock_path=run_root / "snapshot.lock",
    )
    upgraded_bundle = read_snapshot(run_root / "upgraded-v2.json")
    original_bundle = read_snapshot(data / "predictor_v1.json")
    downgrade = migrate_v2_to_v1(
        data / "predictor_v2.json",
        run_root / "downgraded-v1.json",
        lock_path=run_root / "snapshot.lock",
    )

    unsupported_path = run_root / "unknown-version.json"
    atomic_write_bytes(
        unsupported_path,
        canonical_json({"version": "v99", "payload": {}}, newline=True).encode(
            "utf-8"
        ),
    )
    unsupported = snapshot_error_replay(unsupported_path)

    state_path = run_root / "checkpoint.json"
    state = StageStateMachine(state_path, lock_path=run_root / "state.lock")
    state.commit_stage("ingest", records=("records:fixed-seed",))
    state.commit_stage("align", records=(f"alignment:{first['alignment']['digest']}",))
    savepoint = state.savepoint("pre-exit")
    state.enter_exit_report("SIGTERM")
    exit_resume = state.resume()
    restored = state.restore_savepoint("pre-exit")
    state.commit_stage("score", records=("windows:replayed",))
    state.commit_stage("retrain", records=("closures:replayed",))
    state.commit_stage("ledger", records=("ledger:replayed",))
    state.commit_stage("done", state="done")
    final_state = state.resume()
    stale_temp = state_path.parent / f".{state_path.name}.orphan.tmp"
    atomic_write_bytes(stale_temp, b'{"half":"written"}')
    after_stale = state.resume()

    busy = _lease_probe(run_root / "busy.lock")

    threshold = config.thresholds
    threshold_samples = [
        config.predictor.drift_threshold,
        config.predictor.drift_threshold - 1e-12,
        config.predictor.drift_threshold + 1e-12,
        config.predictor.drift_threshold + 1e-9,
        config.predictor.drift_threshold + 1e-6,
    ]
    precision_probes = []
    for value in threshold_samples:
        decision = compare_threshold(
            value,
            config.predictor.drift_threshold,
            threshold.policy,
            config=threshold,
        )
        precision_probes.append(
            {
                "value": value,
                "exceeds": decision.exceeds,
                "decisions": decision.precision_decisions,
                "fallback": decision.reference_fallback,
            }
        )
    subnormal = compare_threshold(
        1e-320,
        config.predictor.drift_threshold,
        threshold.policy,
        config=threshold,
    )

    checks = {
        "snapshot_generations_consistent": comparison["ok"],
        "ledger_replay_ok": replay["ok"],
        "cache_run_reused": (
            second["cache"]["run_hit"]
            and not second["cache"]["recomputed_windows"]
        ),
        "cache_targeted_invalidation": (
            revision["cache"]["recomputed_windows"] == ["w0001", "w0002", "w0003", "w0004"]
            and revision["cache"]["reused_windows"] == ["w0000"]
        ),
        "upgrade_metadata_completed": (
            upgraded_bundle.version == "v2"
            and upgraded_bundle.current.coefficients
            == original_bundle.current.coefficients
            and bool(upgrade.completions)
        ),
        "downgrade_records_drops": (
            downgrade.target_version == "v1"
            and any(
                item.reason == "E_INCREMENTAL_RETRAIN_DROPPED"
                for item in downgrade.drops
            )
        ),
        "unknown_version_error": (
            unsupported.get("code") == E_VERSION_UNSUPPORTED
        ),
        "exit_report_resume": (
            exit_resume.get("exit_requested") is True
            and exit_resume.get("state") == "exit_report"
            and restored.get("state") == "running"
            and restored.get("exit_requested") is False
        ),
        "atomic_checkpoint_ignores_half_temp": (
            after_stale["version"] == final_state["version"]
            and after_stale["checksum"] == final_state["checksum"]
        ),
        "exclusive_commit_busy_error": busy["ok"],
        "threshold_precision_consistent": all(
            len(set(probe["decisions"].values())) == 1
            for probe in precision_probes
        ),
        "subnormal_protected": (
            subnormal.value.subnormal_zeroed
            and not subnormal.exceeds
        ),
    }
    verification = {
        "schema": "telemetry-drift-verification-v1",
        "ok": all(checks.values()),
        "checks": checks,
        "comparison": comparison,
        "run_summary": first["summary"],
        "run_digest": first["comparison_digest"],
        "cache": {
            "first": first["cache"],
            "second": second["cache"],
            "revision": revision["cache"],
        },
        "ledger": replay,
        "migrations": {
            "upgrade": upgrade.to_dict(),
            "downgrade": downgrade.to_dict(),
        },
        "error_codes": {
            "unknown_version": unsupported,
            "stage_busy": busy,
        },
        "checkpoint": {
            "exit_resume": {
                key: exit_resume.get(key)
                for key in ("version", "state", "exit_requested", "pending_exit")
            },
            "restored": {
                key: restored.get(key)
                for key in ("version", "state", "exit_requested", "restored_from")
            },
            "final": {
                key: final_state.get(key)
                for key in ("version", "completed_stages", "completed_records")
            },
        },
        "threshold_probes": {
            "policy": threshold.policy,
            "samples": precision_probes,
            "subnormal": {
                "exceeds": subnormal.exceeds,
                "value": subnormal.value.to_dict(),
            },
        },
        "data_manifest_sha256": (
            sha256_file(data / "dataset_manifest.json")
            if (data / "dataset_manifest.json").exists()
            else None
        ),
        "run_root": str(run_root),
    }
    atomic_write_json(output / "verification.json", verification)
    _write_run_artifacts(first, output / "latest-run")
    print(json.dumps(verification, ensure_ascii=False, indent=2, sort_keys=True))
    return 0 if verification["ok"] else 1


def _migrate_command(args: argparse.Namespace) -> int:
    bundle = read_snapshot(args.input)
    if bundle.version == "v1":
        result = migrate_v1_to_v2(
            args.input,
            args.output,
            epoch_ms=args.epoch_ms,
            lock_path=args.lock,
        )
    else:
        result = migrate_v2_to_v1(
            args.input,
            args.output,
            lock_path=args.lock,
        )
    print(json.dumps(result.to_dict(), ensure_ascii=False, sort_keys=True))
    return 0


def _replay_ledger_command(args: argparse.Namespace) -> int:
    config = PipelineConfig.load(args.config)
    result = replay_ledger(read_ledger(args.ledger), config)
    print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
    return 0 if result["ok"] else 1


def _checkpoint_command(args: argparse.Namespace) -> int:
    result = checkpoint_error_replay(args.path)
    print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
    return 0 if result["ok"] else 1


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="telemetry-drift")
    subparsers = parser.add_subparsers(dest="command", required=True)

    generate = subparsers.add_parser("generate", help="generate fixed-seed data")
    generate.add_argument("--out", default=str(_default_data_dir()))

    run = subparsers.add_parser("run", help="run the drift pipeline")
    run.add_argument("--config", default="data/predictor_config.json")
    run.add_argument("--observations", default="data/observations.jsonl")
    run.add_argument("--snapshot", default="data/predictor_v1.json")
    run.add_argument("--out", default="artifacts/run")
    run.add_argument("--cache-dir", default=".cache")
    run.add_argument("--no-cache", action="store_true")
    run.add_argument("--changes")
    run.add_argument("--checkpoint")
    run.add_argument("--lock")

    verify = subparsers.add_parser("verify", help="run acceptance checks")
    verify.add_argument("--data", default=str(_default_data_dir()))
    verify.add_argument("--out", default="artifacts/verification")
    verify.add_argument("--regenerate", action="store_true")

    migrate = subparsers.add_parser("migrate-snapshot", help="migrate v1<->v2")
    migrate.add_argument("--input", required=True)
    migrate.add_argument("--output", required=True)
    migrate.add_argument("--epoch-ms", type=int, default=0)
    migrate.add_argument("--lock")

    replay = subparsers.add_parser("replay-ledger", help="replay a ledger file")
    replay.add_argument("--config", default="data/predictor_config.json")
    replay.add_argument("--ledger", required=True)

    checkpoint = subparsers.add_parser("checkpoint", help="inspect a checkpoint")
    checkpoint.add_argument("--path", required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.command == "generate":
        manifest = generate_dataset(args.out)
        print(json.dumps(manifest, ensure_ascii=False, sort_keys=True))
        return 0
    if args.command == "run":
        return _run_command(args)
    if args.command == "verify":
        return _verify_command(args)
    if args.command == "migrate-snapshot":
        return _migrate_command(args)
    if args.command == "replay-ledger":
        return _replay_ledger_command(args)
    if args.command == "checkpoint":
        return _checkpoint_command(args)
    parser.error(f"unknown command {args.command}")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())

