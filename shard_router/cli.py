from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Sequence

from .engine import Engine, parse_params
from .errors import BusinessError, fail
from .util import dump_json


def _add_common(
    parser: argparse.ArgumentParser,
    *,
    injection: bool = True,
    suppress_defaults: bool = False,
) -> None:
    default = argparse.SUPPRESS if suppress_defaults else None
    parser.add_argument(
        "--data-dir",
        default=default,
        help="sample and state directory (default: data)",
    )
    parser.add_argument(
        "--compact",
        action="store_true",
        default=argparse.SUPPRESS if suppress_defaults else False,
        help="emit compact deterministic JSON",
    )
    if injection:
        parser.add_argument(
            "--injections",
            default=default,
            help="path to a local injection manifest",
        )
        parser.add_argument(
            "--no-injection",
            action="store_true",
            default=argparse.SUPPRESS if suppress_defaults else False,
            help="disable injection matching for this command",
        )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="shard-router",
        description="Local deterministic shard routing, rewrite, execution and recovery",
    )
    _add_common(parser)
    subparsers = parser.add_subparsers(dest="command", required=True)

    load_parser = subparsers.add_parser("load", help="validate samples and initialize shards")
    _add_common(load_parser, suppress_defaults=True)
    load_parser.add_argument("--source", choices=["merged", "v1", "v2"], default="merged")
    load_parser.add_argument("--now-ms", type=int, default=None)
    load_parser.add_argument("--v1-base-clock-ms", type=int, default=None)

    route_parser = subparsers.add_parser("route", help="route and rewrite without execution")
    _add_common(route_parser, suppress_defaults=True)
    route_parser.add_argument("--template", required=True)
    route_parser.add_argument("--params", default=None)
    route_parser.add_argument("--source", choices=["merged", "v1", "v2"], default="merged")
    route_parser.add_argument("--now-ms", type=int, default=None)
    route_parser.add_argument("--v1-base-clock-ms", type=int, default=None)

    query_parser = subparsers.add_parser("query", help="execute a logical template")
    _add_common(query_parser, suppress_defaults=True)
    query_parser.add_argument("--template", required=True)
    query_parser.add_argument("--params", default=None)
    query_parser.add_argument("--query-id", default=None)
    query_parser.add_argument("--source", choices=["merged", "v1", "v2"], default="merged")
    query_parser.add_argument("--now-ms", type=int, default=None)
    query_parser.add_argument("--v1-base-clock-ms", type=int, default=None)
    query_parser.add_argument("--retry-failed", action="store_true")

    apply_parser = subparsers.add_parser("apply", help="atomically apply a rule snapshot")
    _add_common(apply_parser, injection=False, suppress_defaults=True)
    apply_parser.add_argument(
        "--rules",
        default=None,
        help="v1 JSON file or v2 directory (default: <data-dir>/rules/v2)",
    )
    apply_parser.add_argument("--version", default=None)
    apply_parser.add_argument("--now-ms", type=int, default=None)
    apply_parser.add_argument("--v1-base-clock-ms", type=int, default=None)

    resume_parser = subparsers.add_parser("resume", help="resume committed-safe work")
    _add_common(resume_parser, suppress_defaults=True)
    resume_parser.add_argument("--query-id", default=None)
    resume_parser.add_argument("--all", action="store_true")
    resume_parser.add_argument("--now-ms", type=int, default=None)
    resume_parser.add_argument("--v1-base-clock-ms", type=int, default=None)

    status_parser = subparsers.add_parser("status", help="print stable persisted status")
    _add_common(status_parser, injection=False, suppress_defaults=True)
    status_parser.add_argument("--query-id", default=None)

    verify_parser = subparsers.add_parser("verify", help="verify snapshots and persisted state")
    _add_common(verify_parser, injection=False, suppress_defaults=True)
    verify_parser.add_argument("--now-ms", type=int, default=None)
    verify_parser.add_argument("--v1-base-clock-ms", type=int, default=None)
    return parser


def _resolved_value(args: argparse.Namespace, name: str, default: Any) -> Any:
    value = getattr(args, name, None)
    return default if value is None else value


def _engine_from_args(args: argparse.Namespace) -> Engine:
    data_dir = Path(_resolved_value(args, "data_dir", "data"))
    injection_path = (
        Path(args.injections).resolve()
        if getattr(args, "injections", None)
        else None
    )
    return Engine(
        data_dir,
        injection_path=injection_path,
        injections_enabled=not bool(getattr(args, "no_injection", False)),
    )


def execute(args: argparse.Namespace) -> dict[str, Any]:
    engine = _engine_from_args(args)
    if args.command == "load":
        return engine.load(
            source_filter=args.source,
            now_clock_ms=args.now_ms,
            v1_base_clock_ms=args.v1_base_clock_ms,
        )
    if args.command == "route":
        return engine.route(
            template_id=args.template,
            params=parse_params(args.params),
            source_filter=args.source,
            now_clock_ms=args.now_ms,
            v1_base_clock_ms=args.v1_base_clock_ms,
        )
    if args.command == "query":
        return engine.query(
            template_id=args.template,
            params=parse_params(args.params),
            query_id=args.query_id,
            now_clock_ms=args.now_ms,
            source_filter=args.source,
            v1_base_clock_ms=args.v1_base_clock_ms,
            retry_failed=args.retry_failed,
        )
    if args.command == "apply":
        data_dir = Path(_resolved_value(args, "data_dir", "data")).resolve()
        rules_path = (
            Path(args.rules).resolve()
            if args.rules
            else data_dir / "rules" / "v2"
        )
        return engine.apply(
            rules_path=rules_path,
            version=args.version,
            now_clock_ms=args.now_ms,
            v1_base_clock_ms=args.v1_base_clock_ms,
        )
    if args.command == "resume":
        return engine.resume(
            query_id=args.query_id,
            all_queries=args.all or args.query_id is None,
            now_clock_ms=args.now_ms,
            v1_base_clock_ms=args.v1_base_clock_ms,
        )
    if args.command == "status":
        return engine.status(query_id=args.query_id)
    if args.command == "verify":
        return engine.verify(
            now_clock_ms=args.now_ms,
            v1_base_clock_ms=args.v1_base_clock_ms,
        )
    fail("E_INTERNAL", "unknown command", command=args.command)


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    compact = bool(getattr(args, "compact", False))
    try:
        output = execute(args)
        print(dump_json(output, pretty=not compact))
        if output.get("ok") is False:
            return int(output.get("exit_code", 5))
        return 0
    except BusinessError as error:
        payload = error.to_dict()
        payload["command"] = args.command
        print(dump_json(payload, pretty=not compact))
        return error.exit_code
    except KeyboardInterrupt:
        payload = {
            "ok": False,
            "code": "E_INTERRUPTED",
            "message": "command interrupted",
            "command": args.command,
            "exit_code": 130,
        }
        print(dump_json(payload, pretty=not compact))
        return 130
    except Exception as error:  # pragma: no cover - defensive CLI boundary
        payload = {
            "ok": False,
            "code": "E_INTERNAL",
            "message": "unexpected internal error",
            "details": {
                "exception": type(error).__name__,
                "error": str(error),
            },
            "command": args.command,
            "exit_code": 9,
        }
        print(dump_json(payload, pretty=not compact))
        return 9


if __name__ == "__main__":
    raise SystemExit(main())
