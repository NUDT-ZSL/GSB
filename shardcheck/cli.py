from __future__ import annotations

import argparse
import json
import signal
import sys
from pathlib import Path
from typing import Any

from .checkpoints import CheckpointStore
from .config import SampleBundle
from .engine import TrainingEngine
from .errors import ShardCheckError, SnapshotVersionMismatch
from .injection import InjectionTable
from .io import atomic_write_json, read_json, sha256_file
from .model import DeterministicModel
from .report import deterministic_report_view


def _print_json(value: Any, *, stream: Any = sys.stdout) -> None:
    json.dump(value, stream, ensure_ascii=False, sort_keys=True, indent=2)
    stream.write("\n")


def _load_engine(args: argparse.Namespace) -> TrainingEngine:
    samples = Path(args.samples).resolve()
    run_dir = Path(args.run_dir).resolve()
    injection_path = Path(args.injections).resolve() if args.injections else None
    bundle = SampleBundle.load(samples, injections_path=injection_path)
    injections = InjectionTable.load(injection_path)
    return TrainingEngine(
        bundle,
        run_dir,
        injections,
        write_delay_seconds=args.write_delay_seconds,
        round_delay_seconds=args.round_delay_seconds,
        fresh=args.fresh,
    )


def cmd_run(args: argparse.Namespace) -> int:
    try:
        engine = _load_engine(args)
    except ShardCheckError as exc:
        _print_json({"ok": False, "code": exc.code, "message": str(exc)}, stream=sys.stderr)
        return 3

    def handle_signal(signum: int, _frame: Any) -> None:
        engine.request_stop(signum)

    previous_term = signal.signal(signal.SIGTERM, handle_signal)
    previous_int = signal.signal(signal.SIGINT, handle_signal)
    try:
        code, report = engine.run()
    except SnapshotVersionMismatch as exc:
        rejection = {
            "ok": False,
            "code": exc.code,
            "stage": exc.stage,
            "message": str(exc),
            "details": exc.details,
            "config_hash": engine.config.config_hash,
            "policy_hash": engine.policy.policy_hash,
        }
        atomic_write_json(Path(args.run_dir).resolve() / "restore_rejection.json", rejection)
        _print_json(rejection, stream=sys.stderr)
        return 2
    except ShardCheckError as exc:
        failure = {
            "ok": False,
            "code": exc.code,
            "stage": exc.stage,
            "message": str(exc),
            "details": exc.details,
        }
        atomic_write_json(Path(args.run_dir).resolve() / "fatal_error.json", failure)
        _print_json(failure, stream=sys.stderr)
        return 3
    finally:
        signal.signal(signal.SIGTERM, previous_term)
        signal.signal(signal.SIGINT, previous_int)

    summary = {
        "ok": True,
        "run_dir": str(Path(args.run_dir).resolve()),
        "report": str(engine.report_path),
        "report_id": report["report_id"],
        "exit_path": report["exit"]["path"],
        "recovery_mode": report["recovery"]["mode"],
        "round_cursor": report["round_cursor"],
        "final_params_digest": report["final_params_digest"],
        "checkpoint_generations": report["checkpoint_manifest"]["global_generation"],
        "current_manifest_digest": report["checkpoint_manifest"]["manifest_digest"],
        "consistency_all_passed": report["consistency"]["all_passed"],
        "failure_count": len(report["failures"]),
        "injection_hit_count": len(report["injection_hits"]),
    }
    _print_json(summary)
    return code


def cmd_verify(args: argparse.Namespace) -> int:
    try:
        bundle = SampleBundle.load(Path(args.samples).resolve())
        store = CheckpointStore(Path(args.run_dir).resolve() / "checkpoint_store", bundle.config)
        manifest = store.load_manifest()
        model, generation, split_ops = store.verify_and_restore(manifest)
        report_path = Path(args.run_dir).resolve() / "report.json"
        final_digest = None
        if report_path.exists():
            final_digest = read_json(report_path).get("final_params_digest")
        final_params_path = Path(args.run_dir).resolve() / "final_parameters.bin"
        final_file_digest = sha256_file(final_params_path) if final_params_path.exists() else None
        checkpoint_round = int(generation.get("round")) if generation else 0
        replay_matches = generation is None
        if generation is not None:
            replay = DeterministicModel(bundle.config)
            for round_number in range(1, checkpoint_round + 1):
                replay.apply_round(round_number, bundle.data_shards)
            replay_matches = replay.digest == model.digest
        result = {
            "ok": replay_matches and final_file_digest == final_digest,
            "config_hash": bundle.config.config_hash,
            "manifest_digest": manifest.get("manifest_digest"),
            "current_generation": manifest.get("current_generation"),
            "total_bytes": manifest.get("total_bytes"),
            "checkpoint_round": checkpoint_round,
            "checkpoint_replay_matches": replay_matches,
            "restored_params_digest": model.digest if model else None,
            "report_final_params_digest": final_digest,
            "final_parameters_file_digest": final_file_digest,
            "report_final_digest_matches_final_parameters_file": final_file_digest == final_digest,
            "report_digest_matches_checkpoint_snapshot": final_digest
            == (model.digest if model else None),
            "split_operations": split_ops,
        }
    except ShardCheckError as exc:
        _print_json({"ok": False, "code": exc.code, "message": str(exc)}, stream=sys.stderr)
        return 3
    _print_json(result)
    return 0


def cmd_compare(args: argparse.Namespace) -> int:
    left_path = Path(args.left).resolve() / "report.json"
    right_path = Path(args.right).resolve() / "report.json"
    try:
        left = read_json(left_path)
        right = read_json(right_path)
    except Exception as exc:
        _print_json({"ok": False, "code": "E_REPORT_READ", "message": str(exc)}, stream=sys.stderr)
        return 3
    left_view = deterministic_report_view(left)
    right_view = deterministic_report_view(right)
    result = {
        "ok": left_view == right_view,
        "left": str(left_path),
        "right": str(right_path),
        "left_final_params_digest": left.get("final_params_digest"),
        "right_final_params_digest": right.get("final_params_digest"),
        "left_manifest_digest": left.get("checkpoint_manifest", {}).get("manifest_digest"),
        "right_manifest_digest": right.get("checkpoint_manifest", {}).get("manifest_digest"),
        "deterministic_fields_equal": left_view == right_view,
    }
    _print_json(result)
    return 0 if result["ok"] else 1


def cmd_inspect(args: argparse.Namespace) -> int:
    report_path = Path(args.run_dir).resolve() / "report.json"
    try:
        report = read_json(report_path)
    except Exception as exc:
        _print_json({"ok": False, "code": "E_REPORT_READ", "message": str(exc)}, stream=sys.stderr)
        return 3
    summary = {
        "ok": True,
        "report_id": report.get("report_id"),
        "config_hash": report.get("config_hash"),
        "policy_hash": report.get("policy_hash"),
        "round_cursor": report.get("round_cursor"),
        "final_params_digest": report.get("final_params_digest"),
        "current_generation": report.get("checkpoint_manifest", {}).get("current_generation"),
        "total_bytes": report.get("checkpoint_manifest", {}).get("total_bytes"),
        "recovery": report.get("recovery"),
        "exit": report.get("exit"),
        "failure_codes": [item.get("code") for item in report.get("failures", [])],
        "injection_ids": [item.get("injection_id") for item in report.get("injection_hits", [])],
        "consistency": report.get("consistency", {}).get("checks"),
    }
    _print_json(summary)
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="shardcheck",
        description="Deterministic checkpoint management and budget scheduling simulator",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    run_parser = subparsers.add_parser("run", help="run or resume deterministic training")
    run_parser.add_argument("--samples", required=True, help="sample directory")
    run_parser.add_argument("--run-dir", required=True, help="persistent run directory")
    run_parser.add_argument("--injections", help="optional independent injection JSON/YAML")
    run_parser.add_argument("--fresh", action="store_true", help="remove an existing run directory first")
    run_parser.add_argument(
        "--write-delay-seconds",
        type=float,
        default=0.0,
        help="deterministic delay per shard write used for local signal/SIGKILL simulation",
    )
    run_parser.add_argument(
        "--round-delay-seconds",
        type=float,
        default=0.0,
        help="delay between rounds for local signal simulation",
    )
    run_parser.set_defaults(func=cmd_run)

    verify_parser = subparsers.add_parser("verify", help="verify manifest and shard reconstruction")
    verify_parser.add_argument("--samples", required=True)
    verify_parser.add_argument("--run-dir", required=True)
    verify_parser.set_defaults(func=cmd_verify)

    compare_parser = subparsers.add_parser("compare", help="compare deterministic fields of two reports")
    compare_parser.add_argument("left")
    compare_parser.add_argument("right")
    compare_parser.set_defaults(func=cmd_compare)

    inspect_parser = subparsers.add_parser("inspect", help="summarize a run report")
    inspect_parser.add_argument("--run-dir", required=True)
    inspect_parser.set_defaults(func=cmd_inspect)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return int(args.func(args))
