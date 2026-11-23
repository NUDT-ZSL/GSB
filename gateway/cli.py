"""Command line interface for the local inference gateway ledger."""

from __future__ import annotations

import argparse
import json
import os
import sys
from typing import Any

from .canonical import canonical_json, load_json, sha256_obj
from .engine import build_status
from .errors import GatewayError
from .store import Store
from .validation import normalize_clock_steps, normalize_inputs
from .verify import verify_store


FAILPOINTS = [
    "after_mutation",
    "after_snapshot_insert",
    "before_commit",
    "after_commit",
    "load_before_commit",
    "load_after_commit",
]


def _output(payload: Any, *, pretty: bool) -> None:
    if pretty:
        text = json.dumps(
            payload,
            sort_keys=True,
            indent=2,
            ensure_ascii=False,
            allow_nan=False,
        )
    else:
        text = canonical_json(payload)
    print(text)


def _parse_csv_ints(values: list[str] | None) -> list[int]:
    if not values:
        return []
    result: list[int] = []
    for value in values:
        for part in value.split(","):
            part = part.strip()
            if not part:
                continue
            try:
                result.append(int(part))
            except ValueError as exc:
                raise GatewayError(
                    "E_INVALID_COMMAND",
                    f"request id is not an integer: {part}",
                    value=part,
                ) from exc
    return sorted(set(result))


def _parse_csv_strings(values: list[str] | None) -> list[str]:
    if not values:
        return []
    result: list[str] = []
    for value in values:
        for part in value.split(","):
            part = part.strip()
            if part:
                result.append(part)
    return sorted(set(result))


def _parse_force_targets(values: list[str] | None) -> dict[str, str]:
    if not values:
        return {}
    result: dict[str, str] = {}
    for value in values:
        if "=" not in value:
            raise GatewayError(
                "E_INVALID_COMMAND",
                "force target must be REQUEST_ID=REPLICA_ID",
                value=value,
            )
        request_text, replica_id = value.split("=", 1)
        try:
            request_id = int(request_text)
        except ValueError as exc:
            raise GatewayError(
                "E_INVALID_COMMAND",
                "force target request id must be an integer",
                value=value,
            ) from exc
        if not replica_id:
            raise GatewayError(
                "E_INVALID_COMMAND",
                "force target replica id must not be empty",
                request_id=request_id,
            )
        result[str(request_id)] = replica_id
    return result


def _read_outcomes(path: str | None) -> list[dict[str, Any]]:
    if path is None:
        return []
    payload = load_json(path)
    if isinstance(payload, dict):
        payload = payload.get("outcomes")
    if not isinstance(payload, list):
        raise GatewayError(
            "E_INVALID_COMMAND",
            "outcomes file must contain a list or an object with an outcomes list",
            source=path,
        )
    return payload


def _derive_round_id(command: dict[str, Any]) -> str:
    core = {key: value for key, value in command.items() if key != "round_id"}
    return "auto:" + sha256_obj(core)[:20]


def _build_round_command(
    *,
    mode: str,
    step: dict[str, Any],
    defaults: dict[str, Any],
) -> dict[str, Any]:
    command: dict[str, Any] = {
        "kind": mode,
        "clock_ms": step.get("clock_ms", defaults.get("clock_ms")),
        "round_id": step.get("round_id"),
    }
    optional_fields = [
        "budget_ms",
        "abort_overdue",
        "dispatch",
        "recover_only",
        "services",
        "requests",
        "force_targets",
        "max_launches",
        "outcomes",
    ]
    for key in optional_fields:
        if key in step:
            command[key] = step[key]
        elif defaults.get(key) is not None:
            command[key] = defaults[key]
    if command.get("clock_ms") is None:
        raise GatewayError(
            "E_INVALID_COMMAND", "clock_ms is required", field="clock_ms"
        )
    if not command.get("round_id"):
        command["round_id"] = _derive_round_id(command)
    return command


def _load_plan(path: str) -> list[dict[str, Any]]:
    payload = load_json(path)
    normalized = normalize_clock_steps(payload)
    if normalized is None:
        raise GatewayError(
            "E_INVALID_COMMAND", "plan file must contain an object with steps"
        )
    return normalized["steps"]


def _cmd_load(args: argparse.Namespace) -> int:
    services_doc = load_json(args.services)
    requests_doc = load_json(args.requests)
    config_doc = load_json(args.config)
    clock_steps_doc = load_json(args.clock_steps) if args.clock_steps else None
    normalized = normalize_inputs(
        services_doc,
        requests_doc,
        config_doc,
        clock_steps_doc,
    )
    store = Store(args.db)
    result = store.load(normalized)
    _output(result, pretty=args.pretty)
    return 0


def _execute_plan(
    store: Store,
    *,
    mode: str,
    steps: list[dict[str, Any]],
    defaults: dict[str, Any],
) -> list[dict[str, Any]]:
    results: list[dict[str, Any]] = []
    for step in steps:
        command = _build_round_command(mode=mode, step=step, defaults=defaults)
        results.append(store.apply_round(command))
    return results


def _cmd_round(args: argparse.Namespace, mode: str) -> int:
    if not os.path.exists(args.db):
        raise GatewayError(
            "E_NOT_LOADED", "ledger database does not exist", db=args.db
        )
    store = Store(args.db, failpoint=args.failpoint)
    outcomes = _read_outcomes(args.outcomes)
    force_targets = _parse_force_targets(args.force_target)
    defaults: dict[str, Any] = {
        "clock_ms": args.clock_ms,
        "budget_ms": args.budget_ms,
        "abort_overdue": True if args.abort_overdue else False,
        "dispatch": False if args.no_dispatch else True,
        "recover_only": True if args.recover_only else False,
        "services": _parse_csv_strings(args.service),
        "requests": _parse_csv_ints(args.request),
        "force_targets": force_targets,
        "max_launches": args.max_launches,
        "outcomes": outcomes,
    }
    if args.plan:
        steps = _load_plan(args.plan)
        results = _execute_plan(
            store, mode=mode, steps=steps, defaults=defaults
        )
        payload = {
            "ok": True,
            "command": mode,
            "plan": args.plan,
            "rounds": results,
            "ledger_version": results[-1].get("ledger_version")
            if results
            else store.read_state()[1],
        }
    else:
        if args.clock_ms is None:
            raise GatewayError(
                "E_INVALID_COMMAND",
                "--clock-ms is required unless --plan is used",
            )
        step: dict[str, Any] = {"clock_ms": args.clock_ms}
        if args.round_id:
            step["round_id"] = args.round_id
        command = _build_round_command(mode=mode, step=step, defaults=defaults)
        result = store.apply_round(command)
        payload = {
            "ok": True,
            "command": mode,
            "ledger_version": result.get("ledger_version"),
            "round": result,
        }
    _output(payload, pretty=args.pretty)
    return 0


def _cmd_status(args: argparse.Namespace) -> int:
    store = Store(args.db)
    state, version, _ = store.read_state()
    _output(build_status(state, version=version), pretty=args.pretty)
    return 0


def _cmd_verify(args: argparse.Namespace) -> int:
    store = Store(args.db)
    _output(verify_store(store), pretty=args.pretty)
    return 0


def _add_common(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--db", required=True, help="SQLite ledger path")
    parser.add_argument("--pretty", action="store_true", help="pretty JSON output")


def _add_round_parser(subparsers: Any, name: str) -> argparse.ArgumentParser:
    parser = subparsers.add_parser(name)
    _add_common(parser)
    parser.add_argument("--clock-ms", type=int, default=None)
    parser.add_argument("--round-id", default=None)
    parser.add_argument("--budget-ms", type=int, default=None)
    parser.add_argument("--plan", default=None, help="JSON logical clock step plan")
    parser.add_argument(
        "--service",
        action="append",
        default=[],
        help="restrict dispatch to service names (repeat or comma separated)",
    )
    parser.add_argument(
        "--request",
        action="append",
        default=[],
        help="restrict dispatch to request ids (repeat or comma separated)",
    )
    parser.add_argument(
        "--force-target",
        action="append",
        default=[],
        help="deterministic target request_id=replica_id",
    )
    parser.add_argument("--outcomes", default=None, help="outcome JSON file")
    parser.add_argument(
        "--abort-overdue",
        action="store_true",
        help="actively expire in-flight attempts whose logical deadline has passed",
    )
    parser.add_argument(
        "--recover-only",
        action="store_true",
        help="do not start new dispatches during this round",
    )
    parser.add_argument(
        "--no-dispatch",
        action="store_true",
        help="only apply outcomes / advance clock",
    )
    parser.add_argument("--max-launches", type=int, default=None)
    parser.add_argument("--failpoint", choices=FAILPOINTS, default=None)
    return parser


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="gateway",
        description="Deterministic local inference gateway quota ledger",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    load_parser = subparsers.add_parser("load")
    _add_common(load_parser)
    load_parser.add_argument("--services", required=True)
    load_parser.add_argument("--requests", required=True)
    load_parser.add_argument("--config", required=True)
    load_parser.add_argument("--clock-steps", default=None)
    load_parser.set_defaults(func=_cmd_load)

    dispatch_parser = _add_round_parser(subparsers, "dispatch")
    dispatch_parser.set_defaults(func=lambda args: _cmd_round(args, "dispatch"))

    resume_parser = _add_round_parser(subparsers, "resume")
    resume_parser.set_defaults(func=lambda args: _cmd_round(args, "resume"))

    status_parser = subparsers.add_parser("status")
    _add_common(status_parser)
    status_parser.set_defaults(func=_cmd_status)

    verify_parser = subparsers.add_parser("verify")
    _add_common(verify_parser)
    verify_parser.set_defaults(func=_cmd_verify)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return int(args.func(args))
    except GatewayError as exc:
        _output(
            {"ok": False, "error": exc.to_dict()},
            pretty=getattr(args, "pretty", False),
        )
        return 2
    except KeyboardInterrupt:
        _output(
            {
                "ok": False,
                "error": {"code": "E_INTERRUPTED", "message": "interrupted"},
            },
            pretty=getattr(args, "pretty", False),
        )
        return 130
    except Exception as exc:  # pragma: no cover - last-resort diagnostic
        if os.environ.get("GW_DEBUG"):
            raise
        _output(
            {
                "ok": False,
                "error": {
                    "code": "E_INTERNAL",
                    "message": f"{type(exc).__name__}: {exc}",
                },
            },
            pretty=getattr(args, "pretty", False),
        )
        return 1


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
