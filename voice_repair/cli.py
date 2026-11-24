from __future__ import annotations

import argparse
import json
import signal
import sys
import threading
from pathlib import Path

from .engine import BatchEngine, RunOptions
from .errors import (
    EXIT_OK,
    EXIT_TASK_INVALID,
    EXIT_UNEXPECTED,
    LedgerError,
    TaskValidationError,
)
from .samples import generate_demo_dataset
from .task import load_task
from .verify import run_verification


def _task_path(value: str) -> Path:
    return Path(value)


def _add_common_run_paths(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--task", type=_task_path, default=Path("data/task.json"))
    parser.add_argument("--ledger", type=Path, default=Path("state/ledger.sqlite"))
    parser.add_argument("--out-dir", type=Path, default=Path("state/output"))
    parser.add_argument("--report", type=Path, default=Path("state/report.json"))
    parser.add_argument("--state-dir", type=Path, default=Path("state"))


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="voice-repair",
        description="Deterministic resumable batch voice repair",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    generate = subparsers.add_parser("generate", help="generate deterministic demo WAVs")
    generate.add_argument("--data-dir", type=Path, default=Path("data"))
    generate.add_argument("--seed", type=int, default=20261125)
    generate.add_argument("--force", action="store_true")

    validate = subparsers.add_parser("validate", help="validate task JSON and WAV headers")
    validate.add_argument("--task", type=_task_path, default=Path("data/task.json"))

    run = subparsers.add_parser("run", help="run or resume the batch")
    _add_common_run_paths(run)
    run.add_argument("--workers", type=int, default=None)
    run.add_argument("--mode", default="batch")
    run.add_argument(
        "--crash-after-commits",
        type=int,
        default=None,
        help="fault-injection hook: force-exit after N successful ledger commits",
    )

    verify = subparsers.add_parser(
        "verify",
        help="run serial and parallel fresh batches and compare deterministic results",
    )
    verify.add_argument("--task", type=_task_path, default=Path("data/task.json"))
    verify.add_argument("--work-dir", type=Path, default=Path("state/verification"))
    verify.add_argument("--workers", type=int, default=4)
    return parser


def _print_issues(issues: list) -> None:
    for issue in issues:
        print(
            f"{issue.recording_id}: {issue.code}: {issue.message}",
            file=sys.stderr,
        )


def _load(path: Path):
    try:
        return load_task(path)
    except TaskValidationError as exc:
        print(f"{exc.code}: {exc.message}", file=sys.stderr)
        return None


def _command_validate(args: argparse.Namespace) -> int:
    validation = _load(args.task)
    if validation is None:
        return EXIT_TASK_INVALID
    assert validation.task is not None
    _print_issues(validation.issues)
    print(
        json.dumps(
            {
                "task_id": validation.task.task_id,
                "valid_recordings": len(validation.task.recordings),
                "business_codes": [issue.as_dict() for issue in validation.issues],
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return EXIT_OK if not validation.issues else EXIT_TASK_INVALID


def _command_generate(args: argparse.Namespace) -> int:
    try:
        task = generate_demo_dataset(args.data_dir, args.seed, args.force)
    except FileExistsError as exc:
        print(str(exc), file=sys.stderr)
        return EXIT_TASK_INVALID
    print(f"generated deterministic dataset: {task}")
    return EXIT_OK


class _SignalState:
    def __init__(self) -> None:
        self.event = threading.Event()
        self.number: int | None = None

    def handle(self, signum: int, _frame: object) -> None:
        self.number = signum
        self.event.set()

    def get_number(self) -> int | None:
        return self.number


def _command_run(args: argparse.Namespace) -> int:
    signal_state = _SignalState()
    previous_handlers: dict[int, object] = {}
    signals = [signal.SIGINT, signal.SIGTERM]
    if hasattr(signal, "SIGBREAK"):
        signals.append(signal.SIGBREAK)
    for signum in signals:
        try:
            previous_handlers[signum] = signal.getsignal(signum)
            signal.signal(signum, signal_state.handle)
        except (OSError, ValueError):
            pass

    try:
        validation = _load(args.task)
        if validation is None or validation.task is None:
            return EXIT_TASK_INVALID
        _print_issues(validation.issues)
        engine = BatchEngine(
            task=validation.task,
            validation_issues=validation.issues,
            ledger_path=args.ledger,
            output_dir=args.out_dir,
            report_path=args.report,
            state_dir=args.state_dir,
            options=RunOptions(
                mode=args.mode,
                workers=args.workers,
                crash_after_commits=args.crash_after_commits,
                stop_event=signal_state.event,
                signal_number=signal_state.get_number,
            ),
        )
        outcome = engine.run()
    except LedgerError as exc:
        print(f"{exc.code}: {exc.message}", file=sys.stderr)
        return 4
    finally:
        for signum, handler in previous_handlers.items():
            try:
                signal.signal(signum, handler)
            except (OSError, ValueError):
                pass

    print(
        json.dumps(
            {
                "status": outcome.status,
                "exit_code": outcome.exit_code,
                "run_id": outcome.run_id,
                "report": str(outcome.report_path),
                "summary": outcome.report["summary"],
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return outcome.exit_code


def _command_verify(args: argparse.Namespace) -> int:
    validation = _load(args.task)
    if validation is None or validation.task is None:
        return EXIT_TASK_INVALID
    _print_issues(validation.issues)
    result = run_verification(
        validation=validation,
        work_dir=args.work_dir,
        workers=args.workers,
    )
    summary = {
        "ok": result["ok"],
        "checks": result["checks"],
        "max_abs_sample_delta": result["max_abs_sample_delta"],
        "compared_outputs": result["compared_outputs"],
        "verification": result["verification_path"],
    }
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return EXIT_OK if result["ok"] else EXIT_TASK_INVALID


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        if args.command == "generate":
            return _command_generate(args)
        if args.command == "validate":
            return _command_validate(args)
        if args.command == "run":
            return _command_run(args)
        if args.command == "verify":
            return _command_verify(args)
    except KeyboardInterrupt:
        print("interrupted", file=sys.stderr)
        return 130
    except Exception as exc:
        print(f"{type(exc).__name__}: {exc}", file=sys.stderr)
        return EXIT_UNEXPECTED
    return EXIT_UNEXPECTED
