"""Command-line entry points."""

from __future__ import annotations

import argparse
import json
import signal
import sys
from pathlib import Path

from .bundle import first_loaded_source, load_bundle, load_commands
from .engine import Engine
from .jsonutil import dumps_canonical
from .samplegen import generate_samples


def _print(value) -> None:
    sys.stdout.write(dumps_canonical(value, newline=True))


def _summary(result) -> dict:
    state = result.state
    return {
        "exit_code": result.exit_code,
        "snapshot_hit": result.snapshot_hit,
        "stopped_by_signal": result.stopped_by_signal,
        "processed_commands": result.processed_commands,
        "last_operation_index": state.last_operation_index if state else None,
        "global_version": state.global_version if state else None,
        "source_version": state.source_version if state else None,
        "target_version": state.target_version if state else None,
        "ir_fingerprint": state.ir_fingerprint if state else None,
        "loss_hash": state.loss_hash if state else None,
        "failure_count": len(state.failures) if state else 0,
        "injection_hits": (
            sum(1 for item in result.report.get("injections_hit", []) if item.get("hit"))
            if result.report
            else 0
        ),
    }


def _run(args: argparse.Namespace) -> int:
    bundle = load_bundle(args.bundle)
    commands_path = Path(args.commands) if args.commands else bundle.commands_path
    commands = load_commands(commands_path)
    source_version = args.source_version or first_loaded_source(
        commands, bundle.default_source_version
    )
    engine = Engine(
        bundle,
        args.workspace,
        max_workers=args.max_workers,
    )
    if not args.resume:
        engine.reset_workspace()

    previous_term = signal.getsignal(signal.SIGTERM)
    previous_int = signal.getsignal(signal.SIGINT)

    def handler(signum, _frame):
        engine.request_stop(signum)

    signal.signal(signal.SIGTERM, handler)
    signal.signal(signal.SIGINT, handler)
    try:
        if args.resume:
            engine.prepare(source_version, resume=True)
            resume_skipping = engine.snapshot_hit
        else:
            resume_skipping = False
        result = engine.execute(
            commands,
            resume_skipping=resume_skipping,
            one_command=args.one_command,
            abrupt_after=args.abrupt_after,
            graceful_after=args.graceful_after,
        )
    finally:
        signal.signal(signal.SIGTERM, previous_term)
        signal.signal(signal.SIGINT, previous_int)
    _print(_summary(result))
    return result.exit_code


def _generate(args: argparse.Namespace) -> int:
    result = generate_samples(args.output, seed=args.seed, force=args.force)
    _print(result)
    return 0


def _recover(args: argparse.Namespace) -> int:
    from .snapshots import SnapshotStore

    info = SnapshotStore(args.workspace).recovery_info()
    if info is None:
        _print({"valid": False, "error": "no snapshot found"})
        return 1
    _print(info)
    return 0 if info.get("valid") else 1


def _diagnose(args: argparse.Namespace) -> int:
    bundle = load_bundle(args.bundle)
    source_version = args.source_version or bundle.default_source_version
    engine = Engine(bundle, args.workspace, max_workers=args.max_workers)
    engine.reset_workspace()
    commands = [
        {"op": 1, "command_id": "diagnose-load", "type": "load_project", "source_version": source_version},
        {"op": 2, "command_id": "diagnose-exit", "type": "exit"},
    ]
    result = engine.execute(commands)
    _print(_summary(result))
    return result.exit_code


def _verify(args: argparse.Namespace) -> int:
    from .verification import run_verification

    result = run_verification(args.samples, args.output, seed=args.seed)
    _print(
        {
            "passed": result["passed"],
            "check_count": result["check_count"],
            "output": str(Path(args.output).resolve()),
            "injection_hits": result["details"].get("injection_hits", []),
        }
    )
    return 0 if result["passed"] else 1


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="domain-editor",
        description="Deterministic v1/v2 relation and event project editor",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    generate = subparsers.add_parser("generate", help="generate deterministic local samples")
    generate.add_argument("--output", default="samples")
    generate.add_argument("--seed", type=int, default=20261117)
    generate.add_argument("--force", action="store_true")
    generate.set_defaults(func=_generate)

    run = subparsers.add_parser("run", help="execute an edit command stream")
    run.add_argument("--bundle", required=True)
    run.add_argument("--workspace", required=True)
    run.add_argument("--commands")
    run.add_argument("--source-version", choices=["v1", "v2"])
    run.add_argument("--max-workers", type=int)
    run.add_argument("--resume", action="store_true")
    run.add_argument("--one-command", action="store_true", help="process at most one command")
    run.add_argument("--abrupt-after", type=int, help="test hook: os._exit after N committed commands")
    run.add_argument("--graceful-after", type=int, help="test hook: request graceful stop after N commands")
    run.set_defaults(func=_run)

    recover = subparsers.add_parser("recover", help="print the last complete snapshot recovery point")
    recover.add_argument("--bundle", help="accepted for script compatibility; not needed to inspect a snapshot")
    recover.add_argument("--workspace", required=True)
    recover.set_defaults(func=_recover)

    diagnose = subparsers.add_parser("diagnose", help="build a deterministic cross-version report")
    diagnose.add_argument("--bundle", required=True)
    diagnose.add_argument("--workspace", required=True)
    diagnose.add_argument("--source-version", choices=["v1", "v2"])
    diagnose.add_argument("--max-workers", type=int)
    diagnose.set_defaults(func=_diagnose)

    verify = subparsers.add_parser("verify", help="run the complete local acceptance suite")
    verify.add_argument("--samples", default="samples")
    verify.add_argument("--output", default="reports")
    verify.add_argument("--seed", type=int, default=20261117)
    verify.set_defaults(func=_verify)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return int(args.func(args))
    except KeyboardInterrupt:
        return 130
    except Exception as exc:
        sys.stderr.write(f"error: {type(exc).__name__}: {exc}\n")
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
