"""Command-line interface for the lock-wait/index diagnostic tool."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from .errors import DiagnosticError, E_INTERNAL
from .generator import DEFAULT_SEED, generate_sample
from .report import build_report, report_json
from .session import create_run, resume_run
from .store import StateStore
from .util import pretty_json, write_json
from .verify import verify_state


def _emit(value) -> None:
    print(pretty_json(value))


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m lockdiag",
        description="Deterministic SQLite lock-wait and index-attribution diagnostics",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    generate = sub.add_parser("generate", help="generate a deterministic local sample")
    generate.add_argument("--out", default="sample", help="sample output directory")
    generate.add_argument("--seed", type=int, default=DEFAULT_SEED)

    run = sub.add_parser("run", help="start a fresh diagnostic run")
    run.add_argument("--sample", default="sample")
    run.add_argument("--state", default="runtime")
    run.add_argument(
        "--crash-at",
        choices=["enqueue", "grant", "abandon"],
        help="test-only failpoint that abruptly terminates the process",
    )

    resume = sub.add_parser("resume", help="recover and continue an interrupted run")
    resume.add_argument("--state", default="runtime")
    resume.add_argument(
        "--sample",
        default=None,
        help="optional source sample for fingerprint verification",
    )
    resume.add_argument(
        "--crash-at",
        choices=["enqueue", "grant", "abandon"],
        help="test-only failpoint that abruptly terminates the process",
    )

    status = sub.add_parser("status", help="show persistent scheduler and lock state")
    status.add_argument("--state", default="runtime")

    verify = sub.add_parser("verify", help="verify persisted invariants and accounting")
    verify.add_argument("--state", default="runtime")
    verify.add_argument("--out", default=None)

    report = sub.add_parser("report", help="emit a stable JSON diagnostic report")
    report.add_argument("--state", default="runtime")
    report.add_argument("--out", default=None)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        if args.command == "generate":
            manifest = generate_sample(args.out, args.seed)
            _emit({"ok": True, "command": "generate", "out": str(Path(args.out).resolve()), "manifest": manifest})
            return 0
        if args.command == "run":
            summary = create_run(args.sample, args.state, crash_at=args.crash_at)
            summary["state"] = str(Path(args.state).resolve())
            _emit(summary)
            return 0
        if args.command == "resume":
            summary = resume_run(args.state, args.sample, crash_at=args.crash_at)
            summary["state"] = str(Path(args.state).resolve())
            _emit(summary)
            return 0
        if args.command == "status":
            with StateStore(args.state) as store:
                _emit({"ok": True, "command": "status", **store.status_payload()})
            return 0
        if args.command == "report":
            with StateStore(args.state) as store:
                value = build_report(store)
                text = pretty_json(value) + "\n"
                if args.out:
                    Path(args.out).parent.mkdir(parents=True, exist_ok=True)
                    Path(args.out).write_text(text, encoding="utf-8", newline="\n")
                sys.stdout.write(text)
            return 0
        if args.command == "verify":
            with StateStore(args.state) as store:
                value = verify_state(store)
                value["command"] = "verify"
                if args.out:
                    write_json(Path(args.out), value)
                _emit(value)
                return 0 if value["ok"] else 1
        raise DiagnosticError(E_INTERNAL, f"unknown command: {args.command}")
    except DiagnosticError as exc:
        _emit({"ok": False, "error": exc.as_dict()})
        return 2
    except KeyboardInterrupt:
        _emit({"ok": False, "error": {"code": "E_INTERRUPTED", "message": "interrupted"}})
        return 130
    except Exception as exc:
        _emit(
            {
                "ok": False,
                "error": {
                    "code": E_INTERNAL,
                    "message": f"{type(exc).__name__}: {exc}",
                },
            }
        )
        return 1
