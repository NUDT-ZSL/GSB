"""Command-line entry points for reconciliation, recovery, and verification."""

from __future__ import annotations

import argparse
import json
import sys
from typing import Any

from .canon import canonical_text, read_json
from .errors import DriftError
from .fixtures import analyze_fixture
from .mock_server import main as mock_main
from .runner import reconcile_service, recover_all
from .store import Database
from .validation import get_service, validate_expected, validate_policy
from .verify import verify_database


def _print(value: Any) -> None:
    sys.stdout.write(canonical_text(value, indent=2))


def _load_validated(expected_path: str, policy_path: str) -> tuple[dict[str, Any], dict[str, Any]]:
    expected = validate_expected(read_json(expected_path))
    policy = validate_policy(read_json(policy_path))
    return expected, policy


def _select_service(expected: dict[str, Any], requested: str | None) -> str:
    if requested is not None:
        get_service(expected, requested)
        return requested
    if len(expected["services"]) != 1:
        raise DriftError(
            "E_BAD_EXPECTED",
            "--service is required when the expected snapshot contains multiple services",
            path="$.services",
        )
    return expected["services"][0]["service"]


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="mesh-drift")
    subparsers = parser.add_subparsers(dest="command", required=True)

    validate_parser = subparsers.add_parser("validate", help="validate snapshots without writing state")
    validate_parser.add_argument("--expected", required=True)
    validate_parser.add_argument("--policy", required=True)

    analyze_parser = subparsers.add_parser(
        "analyze", help="classify and adjudicate an offline report fixture"
    )
    analyze_parser.add_argument("--expected", required=True)
    analyze_parser.add_argument("--policy", required=True)
    analyze_parser.add_argument("--reports", required=True)
    analyze_parser.add_argument("--service")
    analyze_parser.add_argument("--clock", required=True, type=int)

    reconcile_parser = subparsers.add_parser(
        "reconcile", help="fetch reports and atomically commit one service round"
    )
    reconcile_parser.add_argument("--db", required=True)
    reconcile_parser.add_argument("--expected", required=True)
    reconcile_parser.add_argument("--policy", required=True)
    reconcile_parser.add_argument("--service")
    reconcile_parser.add_argument("--clock", required=True, type=int)
    reconcile_parser.add_argument("--round-id")

    recover_parser = subparsers.add_parser(
        "recover", help="resume every uncommitted round journal"
    )
    recover_parser.add_argument("--db", required=True)

    verify_parser = subparsers.add_parser(
        "verify", help="independently replay every committed round and verify ledger chains"
    )
    verify_parser.add_argument("--db", required=True)

    serve_parser = subparsers.add_parser(
        "serve-mock", help="serve deterministic loopback report endpoints"
    )
    serve_parser.add_argument("--expected", required=True)
    serve_parser.add_argument("--scenario", required=True)
    serve_parser.add_argument("--endpoints-output")
    return parser


def run(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        if args.command == "validate":
            expected, policy = _load_validated(args.expected, args.policy)
            _print(
                {
                    "ok": True,
                    "services": [item["service"] for item in expected["services"]],
                    "instances": sum(len(item["instances"]) for item in expected["services"]),
                    "policy_id": policy["policy_id"],
                }
            )
            return 0
        if args.command == "analyze":
            if args.clock < 0:
                raise DriftError(
                    "E_BAD_POLICY", "logical clock must be non-negative", path="--clock"
                )
            expected, policy = _load_validated(args.expected, args.policy)
            service_name = _select_service(expected, args.service)
            result = analyze_fixture(
                expected,
                policy,
                read_json(args.reports),
                service_name,
                args.clock,
            )
            _print({"ok": True, "analysis": result})
            return 0
        if args.command == "reconcile":
            if args.clock < 0:
                raise DriftError(
                    "E_BAD_POLICY", "logical clock must be non-negative", path="--clock"
                )
            expected, policy = _load_validated(args.expected, args.policy)
            service_name = _select_service(expected, args.service)
            database = Database(args.db)
            result = reconcile_service(
                database,
                expected=expected,
                policy=policy,
                service_name=service_name,
                logical_clock_ms=args.clock,
                round_id=args.round_id,
            )
            _print({"ok": True, "round": result})
            return 0
        if args.command == "recover":
            database = Database(args.db)
            results = recover_all(database)
            _print({"ok": True, "recovered_rounds": results})
            return 0
        if args.command == "verify":
            database = Database(args.db)
            result = verify_database(database)
            _print(result)
            return 0 if result["ok"] else 1
        if args.command == "serve-mock":
            return mock_main(args.expected, args.scenario, args.endpoints_output)
        raise RuntimeError(f"unhandled command: {args.command}")
    except DriftError as error:
        _print(error.as_dict())
        return 2
    except FileNotFoundError as error:
        _print(
            {
                "ok": False,
                "code": "E_IO",
                "message": str(error),
                "details": {},
            }
        )
        return 2
    except json.JSONDecodeError as error:
        _print(
            {
                "ok": False,
                "code": "E_BAD_JSON",
                "message": str(error),
                "details": {},
            }
        )
        return 2


def main() -> None:
    raise SystemExit(run())

