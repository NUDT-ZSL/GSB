from __future__ import annotations

import argparse
import json
import os
import sys
from collections.abc import Sequence
from dataclasses import replace
from pathlib import Path
from typing import Any

from .engine import Controller
from .errors import BusinessError, ErrorCode
from .launcher import HTTPControlClient
from .models import InputBundle, load_input_bundle, load_json
from .policy import Policy, parse_policy
from .store import Store


def _atomic_write_json(path: str | Path, payload: Any) -> None:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_name(f".{target.name}.{os.getpid()}.tmp")
    text = json.dumps(
        payload,
        sort_keys=True,
        indent=2,
        ensure_ascii=False,
        allow_nan=False,
    )
    temporary.write_text(text + "\n", encoding="utf-8")
    os.replace(temporary, target)


def _print_json(payload: Any, stream: Any = None) -> None:
    if stream is None:
        stream = sys.stdout
    stream.write(
        json.dumps(
            payload,
            sort_keys=True,
            indent=2,
            ensure_ascii=False,
            allow_nan=False,
        )
        + "\n"
    )


def _add_input_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--services", required=True, help="service catalog JSON")
    parser.add_argument("--nodes", required=True, help="node pool JSON")
    parser.add_argument("--trace", required=True, help="workload trace JSON")


def _add_state_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--db", required=True, help="SQLite state database")
    parser.add_argument("--runtime-dir", required=True, help="lock and runtime directory")


def _load_policy(args: argparse.Namespace) -> Policy:
    raw: dict[str, Any] = {}
    if getattr(args, "policy", None):
        loaded = load_json(args.policy)
        if not isinstance(loaded, dict):
            raise BusinessError(
                ErrorCode.INPUT_SCHEMA,
                "policy JSON must be an object",
            )
        raw = loaded
    policy = parse_policy(raw)
    if getattr(args, "budget", None) is not None:
        if args.budget < 0:
            raise BusinessError(
                ErrorCode.INPUT_SCHEMA,
                "--budget must be non-negative",
            )
        policy = replace(policy, logical_time_budget_ticks=args.budget)
    if getattr(args, "action_timeout", None) is not None:
        if args.action_timeout <= 0:
            raise BusinessError(
                ErrorCode.INPUT_SCHEMA,
                "--action-timeout must be positive",
            )
        policy = replace(policy, action_timeout_seconds=args.action_timeout)
    return policy


def _load_bundle(args: argparse.Namespace) -> InputBundle:
    return load_input_bundle(args.services, args.nodes, args.trace)


def _store(args: argparse.Namespace) -> Store:
    store = Store(args.db, args.runtime_dir)
    store.initialize()
    return store


def _service_filter(args: argparse.Namespace) -> list[str] | None:
    values = getattr(args, "service", None)
    if not values:
        return None
    return sorted(set(values))


def _preflight_round(
    bundle: InputBundle,
    *,
    tick: int,
    services: list[str] | None,
) -> None:
    if tick not in bundle.trace.window_by_tick:
        raise BusinessError(
            ErrorCode.TRACE_WINDOW_MISSING,
            "trace has no observation window for this logical tick",
            details={"tick": tick},
        )
    if services is None:
        return
    unknown = sorted(set(services) - set(bundle.service_map()))
    if unknown:
        raise BusinessError(
            ErrorCode.UNKNOWN_SERVICE,
            "round references unknown services",
            details={"services": unknown},
        )


def _controller(
    args: argparse.Namespace,
    bundle: InputBundle,
    policy: Policy,
) -> Controller:
    store = _store(args)
    launcher = HTTPControlClient(args.launcher_url)
    return Controller(
        bundle=bundle,
        policy=policy,
        store=store,
        launcher=launcher,
    )


def command_validate(args: argparse.Namespace) -> int:
    bundle = _load_bundle(args)
    summary = bundle.summary()
    if args.faults:
        faults = load_json(args.faults)
        if not isinstance(faults, dict):
            raise BusinessError(
                ErrorCode.INPUT_SCHEMA,
                "faults JSON must be an object",
            )
        summary["fault_rules"] = len(faults.get("rules", []))
    summary["ok"] = True
    _print_json(summary)
    return 0


def command_run_round(args: argparse.Namespace) -> int:
    bundle = _load_bundle(args)
    policy = _load_policy(args)
    services = _service_filter(args)
    _preflight_round(bundle, tick=args.tick, services=services)
    controller = _controller(args, bundle, policy)
    result = controller.run_round(
        round_id=args.round_id,
        tick=args.tick,
        service_filter=services,
        budget_ticks=args.budget,
    )
    if args.out:
        _atomic_write_json(args.out, result)
    _print_json(result)
    return 0


def command_run(args: argparse.Namespace) -> int:
    bundle = _load_bundle(args)
    policy = _load_policy(args)
    services = _service_filter(args)
    _preflight_round(
        bundle,
        tick=bundle.trace.windows[0].tick,
        services=services,
    )
    controller = _controller(args, bundle, policy)
    output_root = Path(args.out_dir) if args.out_dir else None
    results: list[dict[str, Any]] = []
    for window in bundle.trace.windows:
        if args.start_tick is not None and window.tick < args.start_tick:
            continue
        if args.end_tick is not None and window.tick > args.end_tick:
            continue
        round_id = f"{args.run_id}--t{window.tick:04d}"
        result = controller.run_round(
            round_id=round_id,
            tick=window.tick,
            service_filter=services,
            budget_ticks=args.budget,
        )
        results.append(result)
        if output_root is not None:
            _atomic_write_json(
                output_root / "rounds" / f"{round_id}.json",
                result,
            )
    summary = {
        "ok": True,
        "run_id": args.run_id,
        "rounds": [result["round_id"] for result in results],
        "last_round": results[-1]["round_id"] if results else None,
        "snapshot": (
            results[-1]["snapshot"] if results else controller.store.get_current_snapshot()
        ),
    }
    if output_root is not None:
        _atomic_write_json(output_root / "summary.json", summary)
        snapshot = controller.store.get_current_snapshot()
        if snapshot is not None:
            _atomic_write_json(output_root / "snapshot.json", snapshot)
    _print_json(summary)
    return 0


def command_snapshot(args: argparse.Namespace) -> int:
    store = _store(args)
    snapshot = store.get_current_snapshot()
    if snapshot is None:
        raise BusinessError(
            ErrorCode.STATE_CORRUPT,
            "no committed snapshot exists",
        )
    if args.out:
        _atomic_write_json(args.out, snapshot)
    _print_json(snapshot)
    return 0


def command_state(args: argparse.Namespace) -> int:
    store = _store(args)
    payload = store.export_state()
    if args.out:
        _atomic_write_json(args.out, payload)
    _print_json(payload)
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="elastic-control",
        description="Deterministic local elastic replica control plane",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    validate = subparsers.add_parser("validate", help="validate local deterministic inputs")
    _add_input_arguments(validate)
    validate.add_argument("--faults", help="optional fault injection JSON")
    validate.set_defaults(func=command_validate)

    run_round = subparsers.add_parser("run-round", help="plan and execute one round")
    _add_input_arguments(run_round)
    _add_state_arguments(run_round)
    run_round.add_argument("--round-id", required=True)
    run_round.add_argument("--tick", required=True, type=int)
    run_round.add_argument("--policy")
    run_round.add_argument("--budget", type=int)
    run_round.add_argument("--action-timeout", type=float)
    run_round.add_argument(
        "--launcher-url",
        default="http://127.0.0.1:18080",
    )
    run_round.add_argument("--service", action="append")
    run_round.add_argument("--out")
    run_round.set_defaults(func=command_run_round)

    run = subparsers.add_parser("run", help="execute the full trace deterministically")
    _add_input_arguments(run)
    _add_state_arguments(run)
    run.add_argument("--run-id", default="local-run")
    run.add_argument("--policy")
    run.add_argument("--budget", type=int)
    run.add_argument("--action-timeout", type=float)
    run.add_argument("--start-tick", type=int)
    run.add_argument("--end-tick", type=int)
    run.add_argument(
        "--launcher-url",
        default="http://127.0.0.1:18080",
    )
    run.add_argument("--service", action="append")
    run.add_argument("--out-dir")
    run.set_defaults(func=command_run)

    snapshot = subparsers.add_parser("snapshot", help="print current snapshot")
    _add_state_arguments(snapshot)
    snapshot.add_argument("--out")
    snapshot.set_defaults(func=command_snapshot)

    state = subparsers.add_parser("state", help="export normalized persistent state")
    _add_state_arguments(state)
    state.add_argument("--out")
    state.set_defaults(func=command_state)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return int(args.func(args))
    except BusinessError as exc:
        _print_json(exc.to_dict(), stream=sys.stderr)
        return 2
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    raise SystemExit(main())

