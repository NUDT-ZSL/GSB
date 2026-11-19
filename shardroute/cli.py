"""Command-line interface for the local shard router."""

from __future__ import annotations

import argparse
import json
import os
import sys
from typing import Any

from .engine import QueryEngine
from .errors import BizError, error_envelope
from .injections import InjectionSet
from .router import build_rewrite, merge_params
from .sample import load_sample
from .state import StateStore
from .verify import verify_state


DEFAULT_SAMPLE = "samples/sample.json"
DEFAULT_STATE = "run/state.db"


def _json_output(value: Any) -> None:
    sys.stdout.write(json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n")


def _clock(args: argparse.Namespace) -> int:
    raw = args.clock
    if raw is None:
        raw = os.environ.get("SHARDROUTE_CLOCK_MS")
    if raw is None:
        raise BizError(
            "E_INVALID_ARGUMENT",
            "logical clock is required via --clock or SHARDROUTE_CLOCK_MS",
        )
    try:
        value = int(raw)
    except (TypeError, ValueError) as exc:
        raise BizError("E_INVALID_ARGUMENT", "clock must be an integer") from exc
    if value < 0:
        raise BizError("E_INVALID_ARGUMENT", "clock must be non-negative")
    return value


def _parse_param_value(raw: str) -> Any:
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        if "," in raw:
            values = []
            for item in raw.split(","):
                try:
                    values.append(json.loads(item))
                except json.JSONDecodeError:
                    values.append(item)
            return values
        return raw


def _params(args: argparse.Namespace) -> dict[str, Any]:
    result: dict[str, Any] = {}
    if args.params_json:
        try:
            parsed = json.loads(args.params_json)
        except json.JSONDecodeError as exc:
            raise BizError(
                "E_INVALID_ARGUMENT",
                "--params-json must contain valid JSON",
                details={"reason": str(exc)},
            ) from exc
        if not isinstance(parsed, dict):
            raise BizError("E_INVALID_ARGUMENT", "--params-json must be a JSON object")
        result.update(parsed)
    for item in args.param or []:
        if "=" not in item:
            raise BizError(
                "E_INVALID_ARGUMENT",
                "--param must use key=value syntax",
                details={"value": item},
            )
        key, raw_value = item.split("=", 1)
        if not key:
            raise BizError("E_INVALID_ARGUMENT", "--param key must not be empty")
        result[key] = _parse_param_value(raw_value)
    return result


def _add_state(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--state", default=DEFAULT_STATE, help="persistent state SQLite path")


def _add_clock(parser: argparse.ArgumentParser, *, required: bool = True) -> None:
    parser.add_argument(
        "--clock",
        required=required,
        help="injectable logical clock in milliseconds",
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="shardroute")
    subparsers = parser.add_subparsers(dest="command", required=True)

    load_parser = subparsers.add_parser("load")
    load_parser.add_argument("--sample", default=DEFAULT_SAMPLE)
    _add_state(load_parser)
    _add_clock(load_parser)

    apply_parser = subparsers.add_parser("apply")
    apply_parser.add_argument("--sample", default=DEFAULT_SAMPLE)
    apply_parser.add_argument("--version", required=True)
    _add_state(apply_parser)
    _add_clock(apply_parser)

    route_parser = subparsers.add_parser("route")
    route_parser.add_argument("--template", required=True)
    route_parser.add_argument("--param", action="append", default=[])
    route_parser.add_argument("--params-json")
    route_parser.add_argument("--injections")
    _add_state(route_parser)
    _add_clock(route_parser)

    query_parser = subparsers.add_parser("query")
    query_parser.add_argument("--template", required=True)
    query_parser.add_argument("--param", action="append", default=[])
    query_parser.add_argument("--params-json")
    query_parser.add_argument("--injections")
    query_parser.add_argument(
        "--crash-at",
        choices=["after_route", "after_rewrite", "after_execute", "after_merge"],
        help="persist the stage marker and force-exit the process (test hook)",
    )
    _add_state(query_parser)
    _add_clock(query_parser)

    resume_parser = subparsers.add_parser("resume")
    _add_state(resume_parser)
    _add_clock(resume_parser)

    status_parser = subparsers.add_parser("status")
    _add_state(status_parser)

    verify_parser = subparsers.add_parser("verify")
    _add_state(verify_parser)
    return parser


def command_load(args: argparse.Namespace) -> dict[str, Any]:
    sample = load_sample(args.sample)
    return StateStore.initialize(args.state, sample, _clock(args))


def command_apply(args: argparse.Namespace) -> dict[str, Any]:
    sample = load_sample(args.sample)
    package = sample.rule(args.version)
    state = StateStore(args.state)
    return state.apply_rule(package, _clock(args))


def command_route(args: argparse.Namespace) -> dict[str, Any]:
    state = StateStore(args.state)
    template = state.query_template(args.template)
    params = merge_params(template, _params(args))
    snapshot = state.read_snapshot()
    rewrite = build_rewrite(snapshot.snapshot, template, params, snapshot.version)
    if args.injections:
        injections = InjectionSet.load(args.injections)
        scenario = injections.find("route", template.id, params)
        if scenario is not None:
            raise BizError(
                scenario.code,
                "injected rule version conflict at routing stage",
                stage="route",
                query_template=template.id,
                target_shards=list(rewrite.target_shards),
                rule_version=snapshot.version,
                details={
                    **scenario.details,
                    "actual_rule_version": snapshot.version,
                    "scenario_id": scenario.id,
                },
            )
        scenario = injections.find("rewrite", template.id, params)
        if scenario is not None:
            raise BizError(
                scenario.code,
                "injected shard key range failure at rewrite stage",
                stage="rewrite",
                query_template=template.id,
                target_shards=list(rewrite.target_shards),
                rule_version=snapshot.version,
                details={**scenario.details, "scenario_id": scenario.id},
            )
    response = rewrite.public_dict()
    response["generation"] = snapshot.generation
    return response


def command_query(args: argparse.Namespace) -> dict[str, Any]:
    injections = InjectionSet.load(args.injections) if args.injections else None
    engine = QueryEngine(
        StateStore(args.state),
        _clock(args),
        injections=injections,
        crash_at=args.crash_at,
    )
    return engine.query(args.template, _params(args))


def command_resume(args: argparse.Namespace) -> dict[str, Any]:
    engine = QueryEngine(StateStore(args.state), _clock(args))
    return engine.resume()


def command_status(args: argparse.Namespace) -> dict[str, Any]:
    return StateStore(args.state).status_payload()


def command_verify(args: argparse.Namespace) -> dict[str, Any]:
    return verify_state(StateStore(args.state))


COMMANDS = {
    "load": command_load,
    "apply": command_apply,
    "route": command_route,
    "query": command_query,
    "resume": command_resume,
    "status": command_status,
    "verify": command_verify,
}


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        result = COMMANDS[args.command](args)
    except BizError as exc:
        _json_output(error_envelope(exc))
        return 1
    _json_output(result)
    if args.command == "verify" and not result.get("ok", False):
        return 1
    if args.command == "resume" and result.get("status") != "OK":
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

