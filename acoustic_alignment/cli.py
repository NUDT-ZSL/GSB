"""Command line interface for the acoustic alignment engine."""
from __future__ import annotations

import argparse
import json
import os
import signal
import sys
from pathlib import Path
from typing import Any

from .codes import Code, EXIT_FATAL, EXIT_INTERRUPTED, EXIT_OK, EXIT_USAGE, EngineError, TaskSchemaError
from .engine import MeasurementEngine, StopController, compute_input_digest
from .ledger import LedgerStore, build_report_from_ledger
from .reporting import commit_report, reconcile_existing_report
from .sample_generator import generate_sample_set
from .state import remove_interruption_marker, write_interruption_marker
from .task_model import load_task


EXIT_ALIGNMENT_ISSUES = 4
EXIT_RECOVERY_FAILED = 3


def _print_json(payload: dict[str, Any]) -> None:
    print(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True))


def install_signal_handlers(controller: StopController) -> None:
    def handler(signum: int, _frame: Any) -> None:
        controller.request("signal", signum=signum)

    for name in ("SIGINT", "SIGTERM", "SIGBREAK", "SIGHUP"):
        sig = getattr(signal, name, None)
        if sig is None:
            continue
        try:
            signal.signal(sig, handler)
        except (ValueError, OSError):
            pass


def _paths(output: str | Path) -> tuple[Path, Path, Path]:
    output_dir = Path(output)
    return (
        output_dir / "alignment_ledger.sqlite",
        output_dir / "alignment_report.json",
        output_dir / "interruption.json",
    )


def cmd_generate(args: argparse.Namespace) -> int:
    _root, task_path = generate_sample_set(args.out, args.seed)
    _print_json({"ok": True, "task": str(task_path), "sample_dir": args.out, "seed": args.seed})
    return EXIT_OK


def cmd_run(args: argparse.Namespace) -> int:
    try:
        task = load_task(args.task)
        if args.workers is not None:
            task.concurrency = max(1, int(args.workers))
        ledger_path, report_path, interruption_path = _paths(args.output)
        stop = StopController()
        install_signal_handlers(stop)
        ledger = LedgerStore(ledger_path)
        input_digest = compute_input_digest(task)
        fault_after = args.fault_exit_after_commit
        if fault_after is None and os.getenv("ACOUSTIC_ALIGNMENT_FAULT_EXIT_AFTER_COMMIT"):
            fault_after = int(os.environ["ACOUSTIC_ALIGNMENT_FAULT_EXIT_AFTER_COMMIT"])

        engine = MeasurementEngine(
            task=task,
            ledger=ledger,
            stop=stop,
            fault_exit_after_commit=fault_after,
        )
        outcome = engine.run(input_digest, resume=not args.force)

        if outcome.interrupted is not None:
            completed = {
                track.track_id
                for track in task.tracks
                if ledger.has_complete_track(input_digest, track.track_id)
            }
            pending = [track.track_id for track in task.tracks if track.track_id not in completed]
            interrupt = outcome.interrupted
            payload = {
                "status": "interrupted",
                "code": "E_INTERRUPTED",
                "exit_code": EXIT_INTERRUPTED,
                "input_digest": input_digest,
                "stage": interrupt.stage,
                "track_id": interrupt.track_id,
                "completed_bands": interrupt.completed_bands,
                "completed_tracks": sorted(completed),
                "pending_track_ids": pending,
                "signal": stop.signum,
                "reason": stop.reason,
            }
            marker = write_interruption_marker(interruption_path, payload)
            _print_json({"ok": False, "interrupted": True, **payload, "marker": str(marker)})
            return EXIT_INTERRUPTED

        if outcome.first_error is not None:
            _print_json(
                {
                    "ok": False,
                    "error": outcome.first_error,
                    "ledger": str(ledger_path),
                    "preserved_complete_tracks": [
                        track.track_id
                        for track in task.tracks
                        if ledger.has_complete_track(input_digest, track.track_id)
                    ],
                }
            )
            return EXIT_FATAL

        recovery = None
        if ledger.restored_from_backup:
            recovery = {"code": Code.E_DIGEST, "restored_valid_ledger_backup": True}
        report = commit_report(report_path, ledger, input_digest, task, extra_recovery=recovery)
        remove_interruption_marker(interruption_path)
        _print_json(
            {
                "ok": True,
                "report": str(report_path),
                "ledger": str(ledger_path),
                "input_digest": input_digest,
                "ledger_digest": ledger.ledger_digest,
                "track_count": report["track_count"],
                "summary": report["summary"],
                "skipped_resume_ids": outcome.skipped_resume_ids,
            }
        )
        if args.strict and (report["summary"]["business_errors"] or report["summary"]["passed"] != report["track_count"]):
            return EXIT_ALIGNMENT_ISSUES
        return EXIT_OK
    except EngineError as exc:
        _print_json({"ok": False, "code": exc.code, "error": exc.message, "details": exc.details})
        return EXIT_RECOVERY_FAILED if exc.code == Code.E_DIGEST else EXIT_FATAL
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        _print_json({"ok": False, "code": Code.E_INTERNAL, "error": str(exc)})
        return EXIT_FATAL


def cmd_verify(args: argparse.Namespace) -> int:
    try:
        task = load_task(args.task)
        ledger_path, report_path, _ = _paths(args.output)
        ledger = LedgerStore(ledger_path)
        input_digest = compute_input_digest(task)
        _rebuilt, recovery = reconcile_existing_report(report_path, ledger, input_digest, task)
        if recovery is None and (args.rebuild or not report_path.is_file()):
            report = build_report_from_ledger(ledger, input_digest, task)
            from .ledger import write_report_atomic

            write_report_atomic(report, report_path)
        else:
            report = json.loads(report_path.read_text(encoding="utf-8"))
        _print_json(
            {
                "ok": True,
                "report": str(report_path),
                "ledger": str(ledger_path),
                "input_digest": input_digest,
                "ledger_digest": ledger.ledger_digest,
                "track_count": report.get("track_count"),
                "recovery": recovery,
            }
        )
        return EXIT_OK
    except EngineError as exc:
        _print_json({"ok": False, "code": exc.code, "error": exc.message, "details": exc.details})
        return EXIT_RECOVERY_FAILED


def cmd_query(args: argparse.Namespace) -> int:
    ledger_path = Path(args.ledger) if args.ledger else Path(args.output) / "alignment_ledger.sqlite"
    ledger = LedgerStore(ledger_path)
    result = ledger.query(track_id=args.track, band_id=args.band, input_digest=args.input_digest)
    _print_json({"ok": True, "ledger": str(ledger_path), **result})
    return EXIT_OK


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="acoustic-alignment", description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)

    generate = subparsers.add_parser("generate", help="generate deterministic local samples")
    generate.add_argument("--out", default="samples")
    generate.add_argument("--seed", type=int, default=380)
    generate.set_defaults(func=cmd_generate)

    run = subparsers.add_parser("run", help="measure tracks and atomically commit report and ledger")
    run.add_argument("--task", "-t", default="samples/measurement_task.json")
    run.add_argument("--output", "-o", default="output")
    run.add_argument("--workers", type=int, default=None)
    run.add_argument("--force", action="store_true", help="remeasure even if complete ledger rows exist")
    run.add_argument("--strict", action="store_true", help="exit 4 when any track fails or has business codes")
    run.add_argument("--fault-exit-after-commit", type=int, default=None, help=argparse.SUPPRESS)
    run.set_defaults(func=cmd_run)

    verify = subparsers.add_parser("verify", help="verify digests and recover/rebuild the report")
    verify.add_argument("--task", "-t", default="samples/measurement_task.json")
    verify.add_argument("--output", "-o", default="output")
    verify.add_argument("--rebuild", action="store_true")
    verify.set_defaults(func=cmd_verify)

    query = subparsers.add_parser("query", help="query ledger by track and/or band")
    query.add_argument("--ledger", default=None)
    query.add_argument("--output", "-o", default="output")
    query.add_argument("--track", default=None)
    query.add_argument("--band", default=None)
    query.add_argument("--input-digest", default=None)
    query.set_defaults(func=cmd_query)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return int(args.func(args))
    except TaskSchemaError as exc:  # pragma: no cover - CLI boundary
        _print_json({"ok": False, "code": exc.code, "error": exc.message, "details": exc.details})
        return EXIT_USAGE


if __name__ == "__main__":
    raise SystemExit(main())
