from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Any, Sequence

from .errors import EventBusError, fail
from .models import load_sample_bundle
from .service import EventBusService
from .util import canonical_json, load_json, parse_json_text


def _add_db(parser: argparse.ArgumentParser, *, required: bool = True) -> None:
    parser.add_argument("--db", required=required, help="SQLite database path")


def _add_expect(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--expect-sample-digest",
        default=None,
        help="reject the operation if the persisted sample digest differs",
    )


def _add_operation_key(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--operation-key",
        default=None,
        help="explicit idempotency key for this mutation",
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="eventbus",
        description="Deterministic local event-bus stream integration middleware",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    load_parser = subparsers.add_parser("load", help="validate and load fixed samples")
    _add_db(load_parser)
    load_parser.add_argument("--samples", default="samples")
    load_parser.add_argument("--clock", type=int, required=True)
    _add_expect(load_parser)
    _add_operation_key(load_parser)

    publish_parser = subparsers.add_parser("publish", help="append unpublished events")
    _add_db(publish_parser)
    publish_parser.add_argument("--clock", type=int, required=True)
    _add_expect(publish_parser)
    _add_operation_key(publish_parser)

    bridge_parser = subparsers.add_parser("bridge", help="run version-bound bridges")
    _add_db(bridge_parser)
    bridge_parser.add_argument("--clock", type=int, required=True)
    bridge_parser.add_argument(
        "--rule",
        action="append",
        default=None,
        help="limit forwarding to this rule id (repeatable)",
    )
    _add_expect(bridge_parser)
    _add_operation_key(bridge_parser)

    consume_parser = subparsers.add_parser("consume", help="pull and checkpoint events")
    _add_db(consume_parser)
    consume_parser.add_argument("--consumer", required=True)
    consume_parser.add_argument("--clock", type=int, required=True)
    consume_parser.add_argument("--budget", type=int, default=None)
    consume_parser.add_argument("--report", type=int, default=None)
    _add_expect(consume_parser)
    _add_operation_key(consume_parser)

    cleanup_parser = subparsers.add_parser(
        "cleanup", help="apply deterministic retention cleanup"
    )
    _add_db(cleanup_parser)
    cleanup_parser.add_argument("--clock", type=int, required=True)
    _add_expect(cleanup_parser)
    _add_operation_key(cleanup_parser)

    edit_parser = subparsers.add_parser(
        "edit", help="versioned event or bridge-rule edit"
    )
    _add_db(edit_parser)
    edit_parser.add_argument("--clock", type=int, required=True)
    edit_parser.add_argument("--kind", choices=["event", "rule"], required=True)
    edit_parser.add_argument(
        "--action", choices=["add", "update", "delete"], required=True
    )
    edit_parser.add_argument("--id", default=None)
    edit_parser.add_argument(
        "--data",
        default=None,
        help="JSON object, or @path to a JSON file",
    )
    _add_expect(edit_parser)
    _add_operation_key(edit_parser)

    resume_parser = subparsers.add_parser(
        "resume", help="replay pending intents that were interrupted"
    )
    _add_db(resume_parser)

    status_parser = subparsers.add_parser("status", help="emit stable status JSON")
    _add_db(status_parser)

    verify_parser = subparsers.add_parser(
        "verify", help="verify log, bridge, checkpoint, cleanup, and config invariants"
    )
    _add_db(verify_parser)
    verify_parser.add_argument("--samples", default=None)
    _add_expect(verify_parser)
    verify_parser.add_argument("--status-file", default=None)

    digest_parser = subparsers.add_parser(
        "digest", help="print the canonical digest of a sample directory"
    )
    digest_parser.add_argument("--samples", default="samples")
    return parser


def _parse_data(value: str | None) -> dict[str, Any] | None:
    if value is None:
        return None
    if value.startswith("@"):
        raw = load_json(value[1:])
    else:
        raw = parse_json_text(value, "--data")
    if not isinstance(raw, dict):
        fail("E_INPUT_JSON", "--data must be a JSON object")
    return raw


def _parse_edit_id(kind: str, value: str | None) -> Any:
    if value is None:
        return None
    if kind == "event":
        try:
            parsed = int(value)
        except ValueError:
            fail("E_EDIT", "event id must be an integer", value=value)
        if parsed <= 0:
            fail("E_EDIT", "event id must be positive", value=value)
        return parsed
    return value


def dispatch(args: argparse.Namespace) -> tuple[dict[str, Any], int]:
    command = args.command
    if command == "digest":
        bundle = load_sample_bundle(args.samples)
        return {
            "ok": True,
            "command": "digest",
            "sample_digest": bundle.digest,
        }, 0

    service = EventBusService(args.db)
    if command == "load":
        bundle = load_sample_bundle(args.samples)
        return service.load(
            bundle,
            args.clock,
            expected_digest=args.expect_sample_digest,
            operation_key=args.operation_key,
        ), 0
    if command == "publish":
        return service.publish(
            args.clock,
            expected_digest=args.expect_sample_digest,
            operation_key=args.operation_key,
        ), 0
    if command == "bridge":
        return service.bridge(
            args.clock,
            rule_ids=args.rule,
            expected_digest=args.expect_sample_digest,
            operation_key=args.operation_key,
        ), 0
    if command == "consume":
        return service.consume(
            args.consumer,
            args.clock,
            budget=args.budget,
            report_offset=args.report,
            expected_digest=args.expect_sample_digest,
            operation_key=args.operation_key,
        ), 0
    if command == "cleanup":
        return service.cleanup(
            args.clock,
            expected_digest=args.expect_sample_digest,
            operation_key=args.operation_key,
        ), 0
    if command == "edit":
        return service.edit(
            args.kind,
            args.action,
            _parse_edit_id(args.kind, args.id),
            _parse_data(args.data),
            args.clock,
            expected_digest=args.expect_sample_digest,
            operation_key=args.operation_key,
        ), 0
    if command == "resume":
        result = service.resume()
        return result, 0 if result["ok"] else 2
    if command == "status":
        return service.status(), 0
    if command == "verify":
        bundle = load_sample_bundle(args.samples) if args.samples else None
        result = service.verify(
            sample_bundle=bundle,
            expect_digest=args.expect_sample_digest,
            status_file=args.status_file,
        )
        return result, 0 if result["ok"] else 2
    fail("E_INPUT", "unknown command", command=command)


def emit(value: dict[str, Any]) -> None:
    sys.stdout.write(canonical_json(value) + "\n")
    sys.stdout.flush()


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        result, exit_code = dispatch(args)
        emit(result)
        return exit_code
    except EventBusError as exc:
        emit(exc.to_dict())
        return 2
    except BrokenPipeError:
        return 1
    except KeyboardInterrupt:
        emit(
            {
                "ok": False,
                "error": {"code": "E_INTERRUPTED", "message": "interrupted"},
            }
        )
        return 130


if __name__ == "__main__":
    raise SystemExit(main())

