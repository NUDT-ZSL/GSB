"""Command-line and interactive command interface."""

from __future__ import annotations

import argparse
import json
import signal
import sys
import threading
from pathlib import Path
from typing import Iterable, TextIO

from .lab import ExperimentSession


def _print_json(payload: object) -> None:
    print(json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2))


def _install_signal_handlers(stop_event: threading.Event) -> None:
    def _handler(signum: int, _frame: object) -> None:
        stop_event.set()
        try:
            name = signal.Signals(signum).name
        except ValueError:
            name = str(signum)
        print(
            f"received {name}; finishing the current round and committing its snapshot",
            file=sys.stderr,
        )

    for signal_name in ("SIGINT", "SIGTERM"):
        signal_value = getattr(signal, signal_name, None)
        if signal_value is not None:
            try:
                signal.signal(signal_value, _handler)
            except (ValueError, OSError):
                pass


def _command_help() -> None:
    print(
        "\n".join(
            [
                "commands:",
                "  list                    show regularization schemes",
                "  scheme <name>           switch active scheme and probe cache reuse",
                "  penalty <non-negative>  change active alpha and probe cache reuse",
                "  mix <0..1>              change active elastic-net L1 ratio",
                "  cache                   probe active cache key",
                "  train                   train active scheme (cache hit skips training)",
                "  train-all               train every configured scheme",
                "  status                  show result summary",
                "  help                    show this help",
                "  quit                    finish and write report",
            ]
        )
    )


def execute_commands(
    session: ExperimentSession,
    lines: Iterable[str],
    *,
    echo_prompt: bool = False,
    pause_after_round: int | None = None,
) -> None:
    for raw_line in lines:
        if session.stop_event.is_set():
            break
        line = raw_line.strip()
        if echo_prompt and line:
            print(f"lab> {line}")
        if not line or line.startswith("#"):
            continue
        parts = line.split()
        command = parts[0].lower()
        arguments = parts[1:]

        if command in {"quit", "exit"}:
            break
        if command in {"help", "?"}:
            _command_help()
            continue
        if command in {"list", "schemes"}:
            _print_json({name: scheme.to_dict() for name, scheme in session.schemes.items()})
            continue
        if command == "scheme":
            if len(arguments) != 1:
                session.record_command_error(command=line, message="scheme requires exactly one name")
                continue
            session.switch_scheme(arguments[0])
            continue
        if command == "penalty":
            if len(arguments) != 1:
                session.record_command_error(command=line, message="penalty requires exactly one number")
                continue
            try:
                value = float(arguments[0])
            except ValueError:
                session.record_command_error(command=line, message="penalty must be numeric")
                continue
            session.set_penalty(value)
            continue
        if command in {"mix", "l1-ratio"}:
            if len(arguments) != 1:
                session.record_command_error(command=line, message="mix requires exactly one number")
                continue
            try:
                value = float(arguments[0])
            except ValueError:
                session.record_command_error(command=line, message="mix must be numeric")
                continue
            session.set_mix(value)
            continue
        if command == "cache":
            _print_json(session.probe_active_cache())
            continue
        if command in {"train", "run", "fit"}:
            result = session.train_active(pause_after_round=pause_after_round)
            if result is None:
                print("training did not start; inspect failures", file=sys.stderr)
                continue
            _print_json(
                {
                    "scheme": result.get("scheme"),
                    "status": result.get("status"),
                    "converged": result.get("converged"),
                    "iterations_completed": result.get("iterations_completed"),
                    "snapshot_version": result.get("snapshot_version"),
                    "cache": result.get("cache_decision"),
                }
            )
            if result.get("status") == "interrupted" and pause_after_round is not None:
                break
            continue
        if command in {"train-all", "run-all"}:
            session.run_all(pause_after_round=pause_after_round)
            _print_json(session.summary())
            continue
        if command == "status":
            _print_json(session.summary())
            continue
        if command == "cache-key":
            _print_json(session.probe_active_cache())
            continue
        session.record_command_error(command=line, message=f"unknown command {command!r}")


def _run_command(args: argparse.Namespace) -> int:
    stop_event = threading.Event()
    _install_signal_handlers(stop_event)
    session = ExperimentSession.from_config_path(
        args.config,
        state_dir=args.state_dir,
        report_path=args.report,
        stop_event=stop_event,
    )

    if args.commands:
        source = sys.stdin if args.commands == "-" else Path(args.commands).expanduser().open("r", encoding="utf-8")
        try:
            execute_commands(
                session,
                source,
                pause_after_round=args.pause_after_round,
            )
        finally:
            if source is not sys.stdin:
                source.close()
    elif args.scheme:
        for name in args.scheme:
            if stop_event.is_set():
                break
            result = session.run_scheme(name, pause_after_round=args.pause_after_round)
            if result is not None and result.get("status") == "interrupted" and args.pause_after_round is not None:
                break
    else:
        session.run_all(pause_after_round=args.pause_after_round)

    try:
        session.write_report()
    except Exception as exc:
        print(f"failed to write report: {exc}", file=sys.stderr)
        return 1

    summary = session.summary()
    _print_json(summary)
    if session.dataset is None:
        return 2
    if stop_event.is_set():
        return 0
    return 0


def _run_interactive(args: argparse.Namespace) -> int:
    stop_event = threading.Event()
    _install_signal_handlers(stop_event)
    session = ExperimentSession.from_config_path(
        args.config,
        state_dir=args.state_dir,
        report_path=args.report,
        stop_event=stop_event,
    )
    _command_help()
    source = sys.stdin
    if args.commands:
        source = sys.stdin if args.commands == "-" else Path(args.commands).expanduser().open("r", encoding="utf-8")
    try:
        if sys.stdin.isatty() and not args.commands:
            def prompt_lines() -> Iterable[str]:
                while True:
                    try:
                        yield input("lab> ")
                    except EOFError:
                        return

            execute_commands(session, prompt_lines(), pause_after_round=args.pause_after_round)
        else:
            execute_commands(
                session,
                source,
                pause_after_round=args.pause_after_round,
            )
    finally:
        if args.commands and source is not sys.stdin:
            source.close()
    session.write_report()
    _print_json(session.summary())
    return 0 if session.dataset is not None else 2


def _run_verify(args: argparse.Namespace) -> int:
    from .verification import run_verification

    result = run_verification(args.config, Path(args.output).expanduser().resolve())
    _print_json(result)
    return 0 if result.get("all_checks_passed", False) else 3


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="regularization-lab",
        description="Deterministic regularized linear-model experiment laboratory",
    )
    subparsers = parser.add_subparsers(dest="command_name", required=True)

    run_parser = subparsers.add_parser("run", help="run configured schemes or a command script")
    run_parser.add_argument("--config", default="configs/experiment.yaml")
    run_parser.add_argument("--state-dir")
    run_parser.add_argument("--report")
    run_parser.add_argument("--commands", help="command script path, or - for stdin")
    run_parser.add_argument("--scheme", action="append", help="run only this scheme; repeatable")
    run_parser.add_argument(
        "--pause-after-round",
        type=int,
        default=None,
        help="deterministic test hook: stop after this absolute round and atomically snapshot",
    )
    run_parser.set_defaults(handler=_run_command)

    interactive_parser = subparsers.add_parser("interactive", help="interactive scheme/cache/training REPL")
    interactive_parser.add_argument("--config", default="configs/experiment.yaml")
    interactive_parser.add_argument("--state-dir")
    interactive_parser.add_argument("--report")
    interactive_parser.add_argument("--commands", help="optional command script path, or - for stdin")
    interactive_parser.add_argument("--pause-after-round", type=int, default=None)
    interactive_parser.set_defaults(handler=_run_interactive)

    verify_parser = subparsers.add_parser("verify", help="run serial/concurrency/recovery/invalidation checks")
    verify_parser.add_argument("--config", default="configs/experiment.yaml")
    verify_parser.add_argument("--output", default="reports/verification.json")
    verify_parser.set_defaults(handler=_run_verify)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return int(args.handler(args))

