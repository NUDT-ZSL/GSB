"""Command-line interface for the local level solver."""

from __future__ import annotations

import argparse
import json
import os
import signal
import sys
import time
from pathlib import Path
from typing import Any, Sequence

from .engine import LevelSolver, SolverInterrupted
from .errors import (
    EXIT_INTERNAL,
    EXIT_INPUT,
    EXIT_OK,
    SolverError,
)
from .model import canonical_json, load_spec
from .samples import conflict_spec_payload, demo_spec_payload
from .snapshots import SnapshotStore

COMMANDS = {"solve", "resume", "verify", "sample"}


def _nonnegative_int(value: str) -> int:
    try:
        number = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("must be an integer") from exc
    if number < 0:
        raise argparse.ArgumentTypeError("must be >= 0")
    return number


def _nonnegative_float(value: str) -> float:
    try:
        number = float(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("must be a number") from exc
    if number < 0:
        raise argparse.ArgumentTypeError("must be >= 0")
    return number


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="level_solver",
        description="Deterministic local level constraint solver",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)
    for command in ("solve", "resume"):
        subparser = subparsers.add_parser(
            command,
            help="solve or resume from persisted snapshots",
        )
        subparser.add_argument("spec", help="level specification JSON")
        subparser.add_argument(
            "--snapshot-dir",
            help="snapshot directory (overrides the specification)",
        )
        subparser.add_argument("--output", help="write the final result JSON here")
        subparser.add_argument(
            "--fresh",
            action="store_true",
            help="archive existing active snapshots and solve from scratch",
        )
        subparser.add_argument(
            "--strict-digest",
            action="store_true",
            help="return E_DIGEST instead of falling back to an earlier snapshot",
        )
        subparser.add_argument(
            "--interrupt-after",
            type=_nonnegative_int,
            default=None,
            help="test hook: stop after this many successfully applied constraints",
        )
        subparser.add_argument(
            "--step-delay-ms",
            type=_nonnegative_float,
            default=0.0,
            help="test hook: yield between constraint boundaries",
        )

    verify = subparsers.add_parser(
        "verify", help="verify snapshot digests and version identifiers"
    )
    verify.add_argument("spec", help="level specification JSON")
    verify.add_argument("--snapshot-dir", help="snapshot directory")

    sample = subparsers.add_parser(
        "sample", help="write a deterministic local sample specification"
    )
    sample.add_argument(
        "kind", choices=("demo", "conflict"), help="sample kind"
    )
    sample.add_argument("output", help="sample JSON output path")
    return parser


def _atomic_write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp-{os.getpid()}")
    try:
        with temporary.open("w", encoding="utf-8", newline="\n") as handle:
            handle.write(canonical_json(payload) + "\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


def _print_json(payload: Any) -> None:
    sys.stdout.write(json.dumps(payload, ensure_ascii=False, indent=2) + "\n")


def _snapshot_dir(spec_dir: str | None, cli_dir: str | None) -> Path:
    if cli_dir:
        return Path(cli_dir)
    if spec_dir:
        return Path(spec_dir)
    return Path("snapshots")


def _run_solve(args: argparse.Namespace) -> int:
    try:
        spec = load_spec(args.spec)
    except SolverError as exc:
        payload = exc.as_dict()
        payload["exit_code"] = exc.exit_code
        _print_json(payload)
        return exc.exit_code
    snapshot_dir = _snapshot_dir(spec.config.snapshot_dir, args.snapshot_dir)
    output_path = Path(args.output) if args.output else (
        Path(spec.config.output) if spec.config.output else None
    )
    stop_requested = False

    def handle_signal(_signum: int, _frame: Any) -> None:
        nonlocal stop_requested
        stop_requested = True

    previous_handlers: dict[int, Any] = {}
    for signum in (signal.SIGINT, signal.SIGTERM):
        try:
            previous_handlers[signum] = signal.getsignal(signum)
            signal.signal(signum, handle_signal)
        except (ValueError, OSError):
            pass

    def should_stop() -> bool:
        if args.step_delay_ms:
            time.sleep(args.step_delay_ms / 1000.0)
        return stop_requested

    try:
        solver = LevelSolver(
            spec,
            snapshot_dir,
            fresh=bool(args.fresh),
            strict_digest=bool(args.strict_digest),
            interrupt_requested=should_stop,
            interrupt_after=args.interrupt_after,
        )
        result = solver.solve().as_dict()
        if output_path is not None:
            _atomic_write_json(output_path, result)
        _print_json(result)
        return EXIT_OK
    except SolverInterrupted as exc:
        payload = exc.as_dict()
        _print_json(payload)
        return exc.exit_code
    except SolverError as exc:
        payload = exc.as_dict()
        payload["exit_code"] = exc.exit_code
        _print_json(payload)
        return exc.exit_code
    except Exception as exc:  # Keep the public boundary machine-readable.
        payload = SolverError(
            "E_INTERNAL", f"unexpected solver failure: {exc}"
        ).as_dict()
        payload["exit_code"] = EXIT_INTERNAL
        _print_json(payload)
        return EXIT_INTERNAL
    finally:
        for signum, handler in previous_handlers.items():
            try:
                signal.signal(signum, handler)
            except (ValueError, OSError):
                pass


def _run_verify(args: argparse.Namespace) -> int:
    try:
        spec = load_spec(args.spec)
    except SolverError as exc:
        payload = exc.as_dict()
        payload["exit_code"] = exc.exit_code
        _print_json(payload)
        return exc.exit_code
    snapshot_dir = _snapshot_dir(spec.config.snapshot_dir, args.snapshot_dir)
    try:
        verification = SnapshotStore(snapshot_dir, spec).verify()
    except SolverError as exc:
        payload = exc.as_dict()
        payload["exit_code"] = exc.exit_code
        _print_json(payload)
        return exc.exit_code
    verification["exit_code"] = 0
    _print_json(verification)
    return EXIT_OK


def _run_sample(args: argparse.Namespace) -> int:
    payload = (
        demo_spec_payload() if args.kind == "demo" else conflict_spec_payload()
    )
    try:
        _atomic_write_json(Path(args.output), payload)
    except OSError as exc:
        error = SolverError("E_IO", f"cannot write sample: {exc}")
        result = error.as_dict()
        result["exit_code"] = error.exit_code
        _print_json(result)
        return error.exit_code
    _print_json({"ok": True, "sample": args.kind, "output": str(args.output)})
    return EXIT_OK


def main(argv: Sequence[str] | None = None) -> int:
    raw_argv = list(sys.argv[1:] if argv is None else argv)
    if raw_argv and raw_argv[0] not in COMMANDS and raw_argv[0] not in {
        "-h",
        "--help",
    }:
        raw_argv.insert(0, "solve")
    parser = build_parser()
    try:
        args = parser.parse_args(raw_argv)
    except SystemExit as exc:
        return int(exc.code)
    if args.command in {"solve", "resume"}:
        return _run_solve(args)
    if args.command == "verify":
        return _run_verify(args)
    if args.command == "sample":
        return _run_sample(args)
    return EXIT_INPUT


if __name__ == "__main__":
    raise SystemExit(main())
