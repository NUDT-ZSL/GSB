"""Command-line interface for the deterministic local batching engine."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Sequence

from . import __version__
from .errors import BusinessError, E_DB_NOT_INITIALIZED, E_JSON_INVALID
from .scheduler import SchedulerEngine
from .status import build_status, status_json, verify_store
from .store import Store
from .training import TrainingEngine
from .validation import load_bundle


DEFAULT_DB = "dynabatch.db"


class CliError(Exception):
    def __init__(self, code: str, message: str, details: dict[str, Any] | None = None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.details = details or {}


def _bundle_path(args: argparse.Namespace) -> str | None:
    explicit = getattr(args, "bundle", None)
    positional = getattr(args, "bundle_path", None)
    if explicit and positional:
        raise CliError(E_JSON_INVALID, "specify bundle either positionally or with --bundle")
    return explicit or positional


def _load_bundle_arg(args: argparse.Namespace):
    path = _bundle_path(args)
    if path is None:
        return None
    return load_bundle(path)


def _open_existing(db: str) -> Store:
    return Store(db, create=False)


def _open_or_initialize(
    db: str, bundle: Any | None
) -> tuple[Store, dict[str, Any] | None]:
    path = Path(db)
    ingest_report = None
    if path.exists():
        store = Store(path, create=False)
    else:
        if bundle is None:
            raise BusinessError(
                E_DB_NOT_INITIALIZED,
                "database does not exist; use save or supply --bundle",
                details={"path": str(path)},
            )
        store = Store(path, create=True)
    if bundle is not None:
        with store.lock():
            ingest_report = store.ingest_bundle(bundle)
    return store, ingest_report


def _now_value(store: Store, requested: int | None) -> int:
    if requested is not None:
        if requested < 0:
            raise CliError(E_JSON_INVALID, "--now must be a non-negative integer")
        return requested
    current = store.logical_clock()
    if current > 0:
        return current
    row = store.fetchone("SELECT MAX(deadline_ms) AS deadline FROM requests")
    return int(row["deadline"]) if row and row["deadline"] is not None else 0


def _print_json(value: Any) -> None:
    print(
        json.dumps(
            value,
            ensure_ascii=False,
            allow_nan=False,
            indent=2,
            sort_keys=True,
        )
    )


def cmd_save(args: argparse.Namespace) -> int:
    bundle = _load_bundle_arg(args)
    if bundle is None:
        raise CliError(E_JSON_INVALID, "save requires --bundle or a positional bundle path")
    path = Path(args.db)
    if path.exists():
        store = Store(path, create=False)
    else:
        store = Store(path, create=True)
    with store:
        with store.lock():
            report = store.ingest_bundle(bundle)
    _print_json({"ok": True, **report})
    return 0


def _run_schedule(args: argparse.Namespace, *, resume: bool) -> int:
    bundle = _load_bundle_arg(args)
    store, ingest_report = _open_or_initialize(args.db, bundle)
    with store:
        now = _now_value(store, args.now)
        with store.lock():
            engine = SchedulerEngine(store, crash_at=args.crash_at)
            result = engine.run(now, max_rounds=args.max_rounds)
        result["ingest"] = ingest_report
        result["resumed"] = bool(resume)
        _print_json({"ok": True, **result})
    return 0


def cmd_schedule(args: argparse.Namespace) -> int:
    return _run_schedule(args, resume=False)


def cmd_resume(args: argparse.Namespace) -> int:
    return _run_schedule(args, resume=True)


def cmd_train(args: argparse.Namespace) -> int:
    bundle = _load_bundle_arg(args)
    store, ingest_report = _open_or_initialize(args.db, bundle)
    with store:
        now = _now_value(store, args.now)
        with store.lock():
            result = TrainingEngine(store).run(
                now,
                fail_batches=tuple(args.fail_batch or ()),
                max_batches=args.max_batches,
            )
        result["ingest"] = ingest_report
        result["state_digest"] = build_status(store)["state_digest"]
        _print_json({"ok": True, **result})
    return 0


def cmd_status(args: argparse.Namespace) -> int:
    store = _open_existing(args.db)
    with store:
        with store.lock():
            output = status_json(store, indent=None if args.canonical else 2)
        if args.output:
            Path(args.output).write_text(output + "\n", encoding="utf-8")
        else:
            print(output)
    return 0


def cmd_verify(args: argparse.Namespace) -> int:
    store = _open_existing(args.db)
    with store:
        with store.lock():
            report = verify_store(store)
            if args.baseline:
                baseline = Store(args.baseline, create=False)
                try:
                    with baseline.lock():
                        baseline_status = build_status(baseline)
                    report["baseline_state_digest"] = baseline_status["state_digest"]
                    report["digests_equal"] = (
                        baseline_status["state_digest"] == report["state_digest"]
                    )
                    if not report["digests_equal"]:
                        report["ok"] = False
                        report["errors"].append(
                            {
                                "check": "baseline_digest",
                                "details": {
                                    "expected": baseline_status["state_digest"],
                                    "actual": report["state_digest"],
                                },
                            }
                        )
                finally:
                    baseline.close()
        _print_json(report)
    return 0 if report["ok"] else 1


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m dynabatch",
        description="Deterministic local inference batching and diagnostics engine",
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    sub = parser.add_subparsers(dest="command", required=True)

    def add_bundle_args(command: argparse.ArgumentParser) -> None:
        command.add_argument("bundle_path", nargs="?", help="validated JSON bundle")
        command.add_argument("--bundle", help="validated JSON bundle")

    save = sub.add_parser("save", help="validate and persist a deterministic bundle")
    save.add_argument("--db", default=DEFAULT_DB)
    add_bundle_args(save)
    save.set_defaults(func=cmd_save)

    schedule = sub.add_parser("schedule", help="advance one logical scheduling workflow")
    schedule.add_argument("--db", default=DEFAULT_DB)
    schedule.add_argument("--now", type=int, help="injected logical clock in milliseconds")
    schedule.add_argument("--max-rounds", type=int, default=10000)
    schedule.add_argument(
        "--crash-at",
        choices=("batch_decision", "window_commit"),
        help="inject a hard process termination at a transaction boundary",
    )
    add_bundle_args(schedule)
    schedule.set_defaults(func=cmd_schedule)

    train = sub.add_parser("train", help="execute committed batches deterministically")
    train.add_argument("--db", default=DEFAULT_DB)
    train.add_argument("--now", type=int, help="injected execution logical clock")
    train.add_argument("--fail-batch", action="append", default=[])
    train.add_argument("--max-batches", type=int)
    add_bundle_args(train)
    train.set_defaults(func=cmd_train)

    resume = sub.add_parser("resume", help="recover partial proposals and continue scheduling")
    resume.add_argument("--db", default=DEFAULT_DB)
    resume.add_argument("--now", type=int, help="injected logical clock in milliseconds")
    resume.add_argument("--max-rounds", type=int, default=10000)
    resume.add_argument(
        "--crash-at",
        choices=("batch_decision", "window_commit"),
        help="inject a hard process termination at a transaction boundary",
    )
    add_bundle_args(resume)
    resume.set_defaults(func=cmd_resume)

    status = sub.add_parser("status", help="emit stable sorted status JSON")
    status.add_argument("--db", default=DEFAULT_DB)
    status.add_argument("--output", help="write status JSON to a file")
    status.add_argument("--canonical", action="store_true", help="compact canonical JSON")
    status.set_defaults(func=cmd_status)

    verify = sub.add_parser("verify", help="verify persisted invariants")
    verify.add_argument("--db", default=DEFAULT_DB)
    verify.add_argument("--baseline", help="compare state digest with another database")
    verify.set_defaults(func=cmd_verify)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return int(args.func(args))
    except BusinessError as exc:
        print(
            json.dumps(
                {"ok": False, "error": exc.as_dict()},
                ensure_ascii=False,
                allow_nan=False,
                sort_keys=True,
            ),
            file=sys.stderr,
        )
        return 2
    except CliError as exc:
        print(
            json.dumps(
                {
                    "ok": False,
                    "error": {
                        "code": exc.code,
                        "message": exc.message,
                        "details": exc.details,
                    },
                },
                ensure_ascii=False,
                allow_nan=False,
                sort_keys=True,
            ),
            file=sys.stderr,
        )
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
