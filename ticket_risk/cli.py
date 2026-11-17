"""Command-line interface and replay driver."""

from __future__ import annotations

import argparse
import json
import os
import select
import signal
import sys
import tempfile
import threading
from pathlib import Path
from typing import Any, TextIO

from .commands import EDIT_COMMANDS, HELP_TEXT, ParsedCommand, parse_command
from .engine import InteractiveEngine
from .errors import E_COMMAND_INVALID, E_COMMAND_UNKNOWN, SnapshotError
from .loader import load_inputs


def _emit(value: dict[str, Any], stream: TextIO = sys.stdout) -> None:
    stream.write(json.dumps(value, ensure_ascii=False, sort_keys=True) + "\n")
    stream.flush()


def _code_from_error(exc: Exception) -> str:
    text = str(exc)
    for code in (E_COMMAND_INVALID, E_COMMAND_UNKNOWN):
        if text.startswith(code) or f": {code}" in text:
            return code
    return E_COMMAND_INVALID


def _ensure_writable_directory(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix=".write-test-", dir=str(path))
    try:
        os.write(fd, b"ok")
        os.fsync(fd)
    finally:
        os.close(fd)
        try:
            os.unlink(name)
        except FileNotFoundError:
            pass


def _run_control(
    engine: InteractiveEngine,
    command: ParsedCommand,
) -> tuple[dict[str, Any], bool]:
    if command.kind == "help":
        return {"ok": True, "event": "help", "text": HELP_TEXT, "budget_excluded": True}, False
    if command.kind == "status":
        return engine.status(), False
    if command.kind == "wait":
        return engine.wait_command(timeout_seconds=120.0), False
    if command.kind == "verify":
        return engine.verify_consistency(timeout_seconds=120.0), False
    if command.kind == "snapshot":
        return engine.force_snapshot(wait=True, timeout_seconds=120.0), False
    if command.kind == "report":
        path = engine.write_report_now()
        return {
            "ok": True,
            "event": "report",
            "report_path": path,
            "budget_excluded": True,
        }, False
    if command.kind == "quit":
        return {"ok": True, "event": "quit-requested", "budget_excluded": True}, True
    raise ValueError(f"{E_COMMAND_UNKNOWN}: unsupported control command: {command.kind}")


def _handle_line(
    engine: InteractiveEngine,
    line: str,
) -> tuple[dict[str, Any] | None, bool]:
    try:
        parsed = parse_command(line)
    except Exception as exc:
        return {
            "ok": False,
            "event": "command_error",
            "command": line.strip(),
            "business_codes": [_code_from_error(exc)],
            "detail": str(exc),
        }, False
    if parsed is None:
        return None, False
    if parsed.kind in EDIT_COMMANDS:
        return engine.apply_edit(parsed), False
    return _run_control(engine, parsed)


def _finalize(
    engine: InteractiveEngine,
    *,
    verify: bool,
    had_command_error: bool,
) -> tuple[dict[str, Any], int]:
    idle = engine.wait_idle(timeout_seconds=180.0)
    if not idle:
        final = {
            "ok": False,
            "event": "final",
            "business_codes": ["E_WAIT_TIMEOUT"],
            "exit_code": 3,
        }
        return final, 3
    verification = None
    if verify:
        verification = engine.verify_consistency(timeout_seconds=180.0)
    engine.force_snapshot(wait=False)
    shutdown = engine.graceful_shutdown(timeout_seconds=180.0)
    inconsistent = bool(verification and not verification.get("consistent"))
    exit_code = 1 if inconsistent or had_command_error else 0
    final = {
        "ok": exit_code == 0,
        "event": "final",
        "exit_code": exit_code,
        "verification": verification,
        "shutdown": shutdown,
        "business_codes": ["E_INCONSISTENT"] if inconsistent else [],
    }
    return final, exit_code


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Chinese ticket sentiment/risk interactive determination demo"
    )
    parser.add_argument("--samples", default="data/samples.jsonl")
    parser.add_argument("--rules", default="resources/rules.json")
    parser.add_argument("--config", default="config/determination.json")
    parser.add_argument("--snapshots", default="snapshots")
    parser.add_argument("--temp", default="temp")
    parser.add_argument("--report", default="reports/diagnostic_report.json")
    parser.add_argument("--replay", default=None)
    parser.add_argument(
        "--fresh",
        action="store_true",
        help="clear snapshot directory before loading (required for a from-zero acceptance run)",
    )
    parser.add_argument(
        "--verify-final",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="for replay/EOF, compare incremental state with a from-scratch serial run",
    )
    args = parser.parse_args(argv)

    try:
        _ensure_writable_directory(Path(args.temp))
        _ensure_writable_directory(Path(args.snapshots))
        _ensure_writable_directory(Path(args.report).parent)
    except OSError as exc:
        _emit(
            {
                "ok": False,
                "event": "startup_error",
                "business_codes": ["E_BAD_RECORD"],
                "detail": f"workspace is not writable: {exc}",
            }
        )
        return 2

    bundle = load_inputs(args.samples, args.rules, args.config)
    if bundle.fatal:
        _emit(
            {
                "ok": False,
                "event": "load_failure",
                "business_codes": sorted(
                    {failure.business_code for failure in bundle.failures}
                ),
                "failures": [failure.to_dict() for failure in bundle.failures],
            }
        )
        return 2

    try:
        engine = InteractiveEngine(
            bundle,
            args.snapshots,
            args.report,
            fresh=args.fresh,
        )
    except SnapshotError as exc:
        _emit(
            {
                "ok": False,
                "event": "startup_refused",
                "business_codes": [exc.code],
                "detail": exc.detail,
                "failures": [failure.to_dict() for failure in bundle.failures],
            }
        )
        return 2
    except Exception as exc:
        _emit(
            {
                "ok": False,
                "event": "startup_error",
                "business_codes": ["E_BAD_RECORD"],
                "detail": f"{type(exc).__name__}: {exc}",
            }
        )
        return 2

    _emit(
        {
            "ok": True,
            "event": "boot",
            "sample_count": len(bundle.samples),
            "load_failures": [failure.to_dict() for failure in bundle.failures],
            "config_hash": bundle.config_hash,
            "resource_fingerprint": bundle.resource_fingerprint,
            "samples_fingerprint": bundle.samples_fingerprint,
            "interaction_budget_ms": bundle.config["interaction_budget_ms"],
            "worker_threads": bundle.config["worker_threads"],
            "snapshot_version": engine.snapshot_version,
            "recovery_info": engine.recovery_info,
            "report": args.report,
        }
    )

    stop_requested = threading.Event()

    def request_stop(signum: int, frame: Any) -> None:
        stop_requested.set()

    old_handlers: dict[int, Any] = {}
    for signum in (signal.SIGTERM, signal.SIGINT):
        try:
            old_handlers[signum] = signal.signal(signum, request_stop)
        except (ValueError, OSError):
            pass

    had_command_error = False
    quit_requested = False
    replay_mode = args.replay is not None
    try:
        if replay_mode:
            with open(args.replay, "r", encoding="utf-8") as handle:
                for line in handle:
                    if stop_requested.is_set():
                        break
                    response, should_quit = _handle_line(engine, line)
                    if response is not None:
                        _emit(response)
                        had_command_error = had_command_error or not response.get("ok", True)
                    if should_quit:
                        quit_requested = True
                        break
        else:
            interactive = sys.stdin.isatty()
            while not stop_requested.is_set():
                if interactive:
                    sys.stderr.write("risk> ")
                    sys.stderr.flush()
                ready, _, _ = select.select([sys.stdin], [], [], 0.05)
                if not ready:
                    continue
                line = sys.stdin.readline()
                if line == "":
                    break
                response, should_quit = _handle_line(engine, line)
                if response is not None:
                    _emit(response)
                    had_command_error = had_command_error or not response.get("ok", True)
                if should_quit:
                    quit_requested = True
                    break
    except KeyboardInterrupt:
        stop_requested.set()
    finally:
        for signum, handler in old_handlers.items():
            try:
                signal.signal(signum, handler)
            except (ValueError, OSError):
                pass

    if stop_requested.is_set() and not replay_mode:
        idle = engine.wait_idle(timeout_seconds=180.0)
        if not idle:
            _emit({"ok": False, "event": "shutdown", "business_codes": ["E_WAIT_TIMEOUT"], "exit_code": 3})
            return 3
        try:
            engine.force_snapshot(wait=False)
            shutdown = engine.graceful_shutdown(timeout_seconds=180.0)
            _emit(shutdown)
            return 0
        except Exception as exc:
            _emit(
                {
                    "ok": False,
                    "event": "shutdown",
                    "business_codes": ["E_SNAPSHOT_INVALID"],
                    "detail": f"{type(exc).__name__}: {exc}",
                    "exit_code": 2,
                }
            )
            return 2

    final, exit_code = _finalize(
        engine,
        verify=args.verify_final and replay_mode,
        had_command_error=had_command_error,
    )
    _emit(final)
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
