"""Stable JSON command-line interface."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
from typing import Any

from .engine import normalize_selection, run_workbench
from .errors import (
    E_SAMPLE_CONFLICT,
    BusinessError,
    as_business_error,
)
from .logging_utils import append_event
from .model import parse_sample_file
from .service import serve
from .storage import Store

PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_SAMPLE = str(PROJECT_ROOT / "samples" / "diagnostic_sample.json")
DEFAULT_DB = ".lbdiag/healthcheck.db"
DEFAULT_LOGS = ".lbdiag/logs"


def _stable_dump(value: Any, pretty: bool) -> str:
    if pretty:
        return json.dumps(value, sort_keys=True, indent=2, ensure_ascii=False, allow_nan=False)
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    )


def _add_common(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--sample", default=DEFAULT_SAMPLE)
    parser.add_argument("--db", default=DEFAULT_DB)
    parser.add_argument("--log-dir", default=DEFAULT_LOGS)
    parser.add_argument("--pretty", action="store_true")
    parser.add_argument("--lock-timeout-ms", type=int, default=30_000)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="lbdiag",
        description="Deterministic local load-balancer health-check diagnostics",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    for name, help_text in (
        ("run", "drive missing checks and commit complete windows"),
        ("resume", "resume from persisted probe results"),
    ):
        sub = subparsers.add_parser(name, help=help_text)
        _add_common(sub)
        sub.add_argument("--now-ms", type=int)
        sub.add_argument("--instance", action="append", default=[])

    status = subparsers.add_parser("status", help="print stable committed state")
    _add_common(status)

    explain = subparsers.add_parser("explain", help="print precision-sensitivity error chains")
    _add_common(explain)
    explain.add_argument("--instance")
    explain.add_argument("--window", type=int)

    verify = subparsers.add_parser("verify", help="verify persisted windows and hashes")
    _add_common(verify)

    serve_parser = subparsers.add_parser("serve", help="run the local loopback test service")
    serve_parser.add_argument("--sample", default=DEFAULT_SAMPLE)
    serve_parser.add_argument("--host")
    serve_parser.add_argument("--port", type=int)
    serve_parser.add_argument("--pretty", action="store_true")
    return parser


def _run_command(args: argparse.Namespace) -> tuple[dict[str, Any], int]:
    config = parse_sample_file(args.sample)
    normalize_selection(config, args.instance)
    if args.lock_timeout_ms <= 0:
        raise BusinessError(
            "E_BAD_DIAGNOSTICS",
            "lock timeout must be positive",
            fields={"lock_timeout_ms": args.lock_timeout_ms},
        )
    crash_limit = None
    raw_crash = os.environ.get("LBDIAG_TEST_CRASH_AFTER_PROBES")
    if raw_crash:
        try:
            crash_limit = int(raw_crash)
        except ValueError as exc:
            raise BusinessError(
                "E_BAD_DIAGNOSTICS",
                "LBDIAG_TEST_CRASH_AFTER_PROBES must be an integer",
            ) from exc
        if crash_limit < 0:
            raise BusinessError(
                "E_BAD_DIAGNOSTICS",
                "LBDIAG_TEST_CRASH_AFTER_PROBES cannot be negative",
            )

    store = Store(args.db, create=True)
    try:
        snapshot = run_workbench(
            store,
            config,
            selected_instances=args.instance,
            now_ms=args.now_ms,
            lock_dir=Path(args.db).parent / "locks",
            lock_timeout_ms=args.lock_timeout_ms,
            crash_after_probes=crash_limit,
        )
        snapshot.pop("execution", None)
        result = {
            "ok": True,
            "command": args.command,
            "result": snapshot,
        }
        append_event(
            args.log_dir,
            {
                "command": args.command,
                "event": "completed",
                "logical_watermark_ms": snapshot["logical_watermark_ms"],
                "sample_sha256": snapshot["sample_sha256"],
                "summary": snapshot["summary"],
            },
        )
        return result, 0
    finally:
        store.close()


def _status_command(args: argparse.Namespace) -> tuple[dict[str, Any], int]:
    input_config = parse_sample_file(args.sample)
    store = Store(args.db, create=False)
    try:
        stored = store.config_sample_sha256()
        if stored != input_config.sample_sha256:
            raise BusinessError(
                E_SAMPLE_CONFLICT,
                "database and sample digests differ",
                fields={
                    "database_sample_sha256": stored,
                    "input_sample_sha256": input_config.sample_sha256,
                },
            )
        config = store.load_config()
        snapshot = store.snapshot(config)
        result = {"ok": True, "command": "status", "result": snapshot}
        append_event(
            args.log_dir,
            {
                "command": "status",
                "event": "read",
                "logical_watermark_ms": snapshot["logical_watermark_ms"],
                "sample_sha256": snapshot["sample_sha256"],
            },
        )
        return result, 0
    finally:
        store.close()


def _explain_command(args: argparse.Namespace) -> tuple[dict[str, Any], int]:
    config = parse_sample_file(args.sample)
    if args.instance and args.instance not in {
        instance.instance_id for instance in config.instances
    }:
        raise BusinessError(
            "E_BAD_INSTANCE",
            "explain instance is not present in the sample",
            fields={"instance_id": args.instance},
        )
    if args.window is not None and args.window < 0:
        raise BusinessError(
            "E_BAD_DIAGNOSTICS",
            "window must be non-negative",
            fields={"window": args.window},
        )
    store = Store(args.db, create=False)
    try:
        if store.config_sample_sha256() != config.sample_sha256:
            raise BusinessError(
                E_SAMPLE_CONFLICT,
                "database and sample digests differ",
                fields={"sample_sha256": config.sample_sha256},
            )
        result = store.explain(
            config, instance_id=args.instance, window_index=args.window
        )
        result = {"ok": True, "command": "explain", "result": result}
        append_event(
            args.log_dir,
            {
                "command": "explain",
                "event": "read",
                "sample_sha256": config.sample_sha256,
            },
        )
        return result, 0
    finally:
        store.close()


def _verify_command(args: argparse.Namespace) -> tuple[dict[str, Any], int]:
    config = parse_sample_file(args.sample)
    store = Store(args.db, create=False)
    try:
        report = store.verify(config)
        result = {
            "ok": bool(report["ok"]),
            "command": "verify",
            "result": report,
        }
        append_event(
            args.log_dir,
            {
                "command": "verify",
                "event": "verified",
                "ok": report["ok"],
                "sample_sha256": config.sample_sha256,
            },
        )
        return result, 0 if report["ok"] else 1
    finally:
        store.close()


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.command == "serve":
        try:
            serve(args.sample, host=args.host, port=args.port)
            return 0
        except BusinessError as exc:
            print(_stable_dump(exc.as_dict(), args.pretty))
            return exc.exit_code
        except OSError as exc:
            error = as_business_error(exc)
            print(_stable_dump(error.as_dict(), args.pretty))
            return 3
    try:
        if args.command in {"run", "resume"}:
            output, code = _run_command(args)
        elif args.command == "status":
            output, code = _status_command(args)
        elif args.command == "explain":
            output, code = _explain_command(args)
        elif args.command == "verify":
            output, code = _verify_command(args)
        else:
            raise BusinessError("E_INTERNAL", f"unknown command {args.command}")
        print(_stable_dump(output, args.pretty))
        return code
    except BusinessError as exc:
        print(_stable_dump(exc.as_dict(), args.pretty))
        return exc.exit_code
    except KeyboardInterrupt:
        return 130
    except BaseException as exc:
        error = as_business_error(exc)
        print(_stable_dump(error.as_dict(), args.pretty))
        return error.exit_code


if __name__ == "__main__":
    raise SystemExit(main())

