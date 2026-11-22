from __future__ import annotations

import argparse
import json
import sys
from typing import Any, Sequence

from .errors import ErrorCode, GatewayError
from .models import canonical_json
from .service import (
    confirm_command,
    deliver_command,
    load_gateway,
    resume_command,
    status_command,
    verify_command,
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="delivery-gateway",
        description="Deterministic SQLite delivery catch-up gateway",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    load = sub.add_parser("load", help="validate configs and load deterministic messages")
    load.add_argument("--db", required=True)
    load.add_argument("--topics", required=True)
    load.add_argument("--consumers", required=True)
    load.add_argument("--quota", required=True)
    load.add_argument("--clock", required=True)
    load.add_argument("--faults", required=True)
    load.add_argument("--pretty", action="store_true")

    deliver = sub.add_parser("deliver", help="run one deterministic catch-up round")
    deliver.add_argument("--db", required=True)
    deliver.add_argument("--consumer", required=True)
    deliver.add_argument("--request-id")
    deliver.add_argument("--now-ms", type=int)
    deliver.add_argument("--limit", type=int)
    deliver.add_argument("--message-id")
    deliver.add_argument("--topic")
    deliver.add_argument("--pretty", action="store_true")

    resume = sub.add_parser("resume", help="replay unfinished claims and running requests")
    resume.add_argument("--db", required=True)
    resume.add_argument("--now-ms", type=int)
    resume.add_argument("--pretty", action="store_true")

    confirm = sub.add_parser("confirm", help="idempotently confirm a delivered message")
    confirm.add_argument("--db", required=True)
    confirm.add_argument("--delivery-id", required=True)
    confirm.add_argument("--consumer-id")
    confirm.add_argument("--now-ms", type=int)
    confirm.add_argument("--pretty", action="store_true")

    status = sub.add_parser("status", help="emit stable sorted state JSON")
    status.add_argument("--db", required=True)
    status.add_argument("--topic")
    status.add_argument("--pretty", action="store_true")

    verify = sub.add_parser("verify", help="replay ledger and generation state")
    verify.add_argument("--db", required=True)
    verify.add_argument("--pretty", action="store_true")
    return parser


def _emit(payload: dict[str, Any], pretty: bool) -> None:
    if pretty:
        text = json.dumps(payload, sort_keys=True, indent=2, ensure_ascii=False)
    else:
        text = canonical_json(payload)
    sys.stdout.write(text + "\n")


def main(argv: Sequence[str] | None = None) -> None:
    parser = build_parser()
    args = parser.parse_args(argv)
    pretty = bool(getattr(args, "pretty", False))
    try:
        if args.command == "load":
            payload = load_gateway(
                args.db,
                topics_path=args.topics,
                consumers_path=args.consumers,
                quota_path=args.quota,
                clock_path=args.clock,
                faults_path=args.faults,
            )
        elif args.command == "deliver":
            payload = deliver_command(
                args.db,
                consumer_id=args.consumer,
                request_id=args.request_id,
                now_ms=args.now_ms,
                limit=args.limit,
                message_id=args.message_id,
                topic=args.topic,
            )
        elif args.command == "resume":
            payload = resume_command(args.db, now_ms=args.now_ms)
        elif args.command == "confirm":
            payload = confirm_command(
                args.db,
                delivery_id=args.delivery_id,
                consumer_id=args.consumer_id,
                now_ms=args.now_ms,
            )
        elif args.command == "status":
            payload = status_command(args.db, topic_filter=args.topic)
        elif args.command == "verify":
            payload = verify_command(args.db)
        else:
            raise GatewayError(ErrorCode.INTERNAL, "unknown command", {"command": args.command})
    except GatewayError as exc:
        _emit(exc.as_dict(), pretty)
        raise SystemExit(exc.exit_code)
    except KeyboardInterrupt:
        _emit(
            GatewayError(ErrorCode.INTERNAL, "interrupted", {}).as_dict(),
            pretty,
        )
        raise SystemExit(130)
    except BaseException as exc:
        _emit(
            GatewayError(
                ErrorCode.INTERNAL,
                "unexpected internal error",
                {"type": type(exc).__name__, "error": str(exc)},
            ).as_dict(),
            pretty,
        )
        raise SystemExit(1)
    _emit(payload, pretty)
    if args.command == "verify" and not payload.get("ok", False):
        raise SystemExit(3)


if __name__ == "__main__":
    main()
