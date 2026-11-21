"""Command line interface for deterministic training and verification."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Sequence

from .config import load_config
from .data import load_dataset
from .engine import TrainingEngine
from .errors import E_ARGUMENT, E_DATA, E_STATE, EngineError, exit_code_for
from .snapshot import SnapshotStore
from .util import canonical_json_text
from .validation import validate_result_document


def _error_payload(error: EngineError) -> str:
    return canonical_json_text(error.to_dict())


def _cmd_train(args: argparse.Namespace) -> int:
    if (args.crash_during_strength is None) != (args.crash_after_iterations is None):
        raise EngineError(
            E_ARGUMENT,
            "--crash-during-strength and --crash-after-iterations must be supplied together",
            stage="cli",
        )
    engine = TrainingEngine(args.config)
    result = engine.run(
        crash_after_committed_strengths=args.crash_after_strengths,
        crash_during_strength=args.crash_during_strength,
        crash_after_iterations=args.crash_after_iterations,
        fault_worker_at=args.fault_worker_at,
    )
    if args.print_result:
        print(canonical_json_text(result))
    else:
        summary = {
            "ok": True,
            "run_id": result["run_id"],
            "result_file": str(engine.config.output_directory / "result.json"),
            "path_checksum": result["summary"]["path_checksum"],
            "n_samples": result["n_samples"],
            "n_features": result["n_features"],
            "completed_strengths": result["summary"]["completed_strengths"],
            "converged_strengths": result["summary"]["converged_strengths"],
            "degradation_categories": result["summary"]["degradation_categories"],
        }
        print(canonical_json_text(summary))
    return 0


def _load_result(path: Path) -> dict[str, Any]:
    try:
        raw = path.read_bytes().decode("utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        raise EngineError(E_DATA, f"cannot read result file {path}: {exc}", stage="validation") from exc
    try:
        value = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise EngineError(
            E_DATA, f"result file is not valid JSON: {exc}", stage="validation"
        ) from exc
    if not isinstance(value, dict):
        raise EngineError(E_DATA, "result file must contain an object", stage="validation")
    return value


def _cmd_validate(args: argparse.Namespace) -> int:
    config = load_config(args.config)
    dataset = load_dataset(config.data)
    document = _load_result(Path(args.result).expanduser().resolve())
    report = validate_result_document(document, config=config, dataset=dataset)
    print(canonical_json_text({"ok": True, "validation": report}))
    return 0


def _cmd_snapshot_status(args: argparse.Namespace) -> int:
    config = load_config(args.config)
    store = SnapshotStore(config.snapshots.directory, config.run_id)
    snapshot = store.load_current()
    if snapshot is None:
        print(canonical_json_text({"ok": True, "committed": False}))
        return 0
    print(
        canonical_json_text(
            {
                "ok": True,
                "committed": True,
                "run_id": snapshot.run_id,
                "version": snapshot.version,
                "stage": snapshot.stage,
                "stage_detail": snapshot.stage_detail,
                "payload_checksum": snapshot.payload_checksum,
                "checksum": snapshot.checksum,
                "snapshot_file": str(snapshot.path),
            }
        )
    )
    return 0


def _cmd_snapshot_rollback(args: argparse.Namespace) -> int:
    config = load_config(args.config)
    store = SnapshotStore(config.snapshots.directory, config.run_id)
    store.request_rollback(args.to_version)
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="linear-path-engine")
    subparsers = parser.add_subparsers(dest="command", required=True)

    train = subparsers.add_parser("train", help="run or resume deterministic path training")
    train.add_argument("--config", "-c", required=True)
    train.add_argument(
        "--crash-after-strengths",
        type=int,
        default=None,
        help="test-only hard exit after N complete strengths are committed",
    )
    train.add_argument(
        "--crash-during-strength",
        type=int,
        default=None,
        help="test-only ascending strength index to interrupt before commit",
    )
    train.add_argument(
        "--crash-after-iterations",
        type=int,
        default=None,
        help="test-only iteration count for the interrupted strength",
    )
    train.add_argument(
        "--fault-worker-at",
        default=None,
        help="test-only worker fault selector '<strength_index>:<block_index>'",
    )
    train.add_argument("--print-result", action="store_true")
    train.set_defaults(handler=_cmd_train)

    validate = subparsers.add_parser("validate", help="recheck a saved result independently")
    validate.add_argument("--config", "-c", required=True)
    validate.add_argument("--result", "-r", required=True)
    validate.set_defaults(handler=_cmd_validate)

    status = subparsers.add_parser("snapshot-status", help="inspect the committed snapshot")
    status.add_argument("--config", "-c", required=True)
    status.set_defaults(handler=_cmd_snapshot_status)

    rollback = subparsers.add_parser("snapshot-rollback", help="attempt a forbidden rollback")
    rollback.add_argument("--config", "-c", required=True)
    rollback.add_argument("--to-version", type=int, required=True)
    rollback.set_defaults(handler=_cmd_snapshot_rollback)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return int(args.handler(args))
    except EngineError as exc:
        print(_error_payload(exc), file=sys.stderr)
        return exit_code_for(exc)
    except KeyboardInterrupt:
        error = EngineError(E_STATE, "training interrupted", stage="cli")
        print(_error_payload(error), file=sys.stderr)
        return 130


if __name__ == "__main__":
    raise SystemExit(main())

