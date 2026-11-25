"""Command line interface for the diagnostic engine."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from .atomicio import atomic_write_json
from .engine import DiagnosticEngine
from .errors import ErrorJournal, DiagnosticError
from .models import load_manifest
from .sample import generate_sample


def _print_json(payload: dict[str, Any]) -> None:
    print(json.dumps(payload, sort_keys=True, indent=2, ensure_ascii=False))


def _write_report(path: str | None, payload: dict[str, Any]) -> None:
    if path:
        report_path = Path(path)
        atomic_write_json(report_path, payload)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="diagnostic-engine")
    subparsers = parser.add_subparsers(dest="command", required=True)

    sample_parser = subparsers.add_parser("sample")
    sample_parser.add_argument("--output", default="samples")

    validate_parser = subparsers.add_parser("validate")
    validate_parser.add_argument("--manifest", required=True)

    run_parser = subparsers.add_parser("run")
    run_parser.add_argument("--manifest", required=True)
    run_parser.add_argument("--state-dir", required=True)
    run_parser.add_argument("--clip-id")
    run_parser.add_argument("--report")
    run_parser.add_argument("--terminate-after-frame", type=int)
    run_parser.add_argument("--sigkill", action="store_true")

    check_parser = subparsers.add_parser("self-check")
    check_parser.add_argument("--state-dir", required=True)
    check_parser.add_argument("--clip-id")
    check_parser.add_argument("--report")

    args = parser.parse_args(argv)

    if args.command == "sample":
        _print_json({"ok": True, "sample": generate_sample(args.output)})
        return 0

    if args.command == "validate":
        validation = load_manifest(args.manifest)
        payload = {
            "ok": validation.ok,
            "task_count": len(validation.tasks),
            "errors": validation.errors,
            "clips": [task.to_dict() for task in validation.tasks],
        }
        _print_json(payload)
        return 0 if validation.ok else 2

    state_dir = Path(args.state_dir)
    state_dir.mkdir(parents=True, exist_ok=True)
    journal = ErrorJournal(state_dir)

    try:
        if args.command == "run":
            validation = load_manifest(args.manifest)
            validation.raise_if_invalid()
            tasks = validation.tasks
            if args.clip_id:
                tasks = [task for task in tasks if task.clip_id == args.clip_id]
                if not tasks:
                    raise DiagnosticError(
                        "E_INPUT_INVALID",
                        "clip_id not found in manifest",
                        clip_id=args.clip_id,
                        field="clip_id",
                    )
            engine = DiagnosticEngine(state_dir)
            clip_reports = []
            for task in tasks:
                clip_reports.append(
                    engine.run_clip(
                        task,
                        terminate_after_frame=args.terminate_after_frame,
                        terminate_with_signal=args.sigkill,
                    )
                )
            payload = {
                "ok": True,
                "report_type": "run",
                "manifest": args.manifest,
                "state_dir": str(state_dir),
                "clips": clip_reports,
                "latest_error": journal.latest(),
            }
            _write_report(args.report, payload)
            _print_json(payload)
            return 0

        if args.command == "self-check":
            engine = DiagnosticEngine(state_dir)
            result = engine.self_check(args.clip_id)
            payload = {
                "ok": True,
                "report_type": "self_check",
                "state_dir": str(state_dir),
                "self_check": result,
                "latest_error": journal.latest(),
            }
            _write_report(args.report, payload)
            _print_json(payload)
            return 0

        parser.error(f"unknown command: {args.command}")
        return 2
    except DiagnosticError as error:
        error_payload = journal.record(error)
        payload = {
            "ok": False,
            "report_type": "error",
            "state_dir": str(state_dir),
            "error": error_payload,
            "latest_error": journal.latest(),
        }
        _write_report(getattr(args, "report", None), payload)
        _print_json(payload)
        return 3 if error.code == "E_STATE_INCONSISTENT" else 2


if __name__ == "__main__":
    raise SystemExit(main())
