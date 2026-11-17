"""Command-line interface for validation, operation, snapshots, and replay."""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path
from typing import Any

from .config import Config, load_config
from .consistency import validate_round
from .engine import HealthController, HealthEngine
from .errors import HealthError
from .ledger import Ledger
from .prober import LoopbackProber
from .recovery import recover_session
from .replay import ReplayVerifier
from .snapshot import SnapshotService


def _emit(value: dict[str, Any] | list[Any]) -> None:
    print(json.dumps(value, indent=2, sort_keys=True, ensure_ascii=False))


def _round_summary(config: Config, result: Any) -> dict[str, Any]:
    check = validate_round(config, result)
    return {
        "round": result.round_no,
        "probes": [probe.deterministic() for probe in result.probes],
        "transitions": [transition.deterministic() for transition in result.transitions],
        "available_members": list(result.available_members),
        "effective_weights": {
            member.id: int(result.effective_weights.get(member.id, 0))
            for member in config.members
        },
        "effective_weight_sum": result.effective_weight_sum,
        "full_weight_sum": result.full_weight_sum,
        "member_runtime": [
            result.state_after.member_states[member.id].to_dict()
            for member in config.members
        ],
        "consistency": check,
    }


def _load_runtime(config_path: str, db_path: str) -> tuple[Config, Ledger, Any, HealthEngine, HealthController, dict[str, Any]]:
    config = load_config(config_path)
    ledger = Ledger(db_path)
    session = recover_session(config, ledger)
    controller = HealthController(config, ledger, session.engine, LoopbackProber(config))
    return config, ledger, session, session.engine, controller, session.report.to_dict()


def cmd_validate(args: argparse.Namespace) -> int:
    try:
        config = load_config(args.config)
    except HealthError as exc:
        _emit(exc.to_dict())
        return 2
    _emit(
        {
            "status": "ok",
            "code": "OK",
            "config_version": config.config_version,
            "member_digest": config.member_digest,
            "config_digest": config.config_digest,
            "full_weight_sum": config.pool.full_weight_sum,
            "members": [
                {
                    "id": member.id,
                    "endpoint": member.endpoint(),
                    "protocol": member.protocol,
                    "weight": member.weight,
                }
                for member in config.members
            ],
        }
    )
    return 0


def cmd_run(args: argparse.Namespace) -> int:
    config, ledger, session, engine, controller, recovery = _load_runtime(
        args.config, args.db
    )
    snapshot_service = SnapshotService(ledger)
    snapshots: list[dict[str, Any]] = []
    rounds: list[dict[str, Any]] = []
    next_tick = time.monotonic()
    interval_s = config.pool.interval_ms / 1000.0
    for offset in range(args.rounds):
        if offset > 0 and args.fixed_period:
            delay = next_tick - time.monotonic()
            if delay > 0:
                time.sleep(delay)
        result = controller.run_round()
        next_tick += interval_s
        if next_tick < time.monotonic():
            next_tick = time.monotonic()
        rounds.append(_round_summary(config, result))
        if args.snapshot_every > 0 and (offset + 1) % args.snapshot_every == 0:
            snapshot = snapshot_service.create(
                config,
                engine,
                label=f"auto-round-{result.round_no}",
                window=args.snapshot_window,
            )
            snapshots.append(snapshot.summary())
    _emit(
        {
            "status": "ok" if all(item["consistency"]["status"] == "ok" for item in rounds) else "error",
            "code": "OK" if all(item["consistency"]["status"] == "ok" for item in rounds) else "E_CONSISTENCY",
            "recovery": recovery,
            "rounds": rounds,
            "snapshots": snapshots,
        }
    )
    return 0 if all(item["consistency"]["status"] == "ok" for item in rounds) else 1


def cmd_status(args: argparse.Namespace) -> int:
    config = load_config(args.config)
    ledger = Ledger(args.db)
    session = recover_session(config, ledger)
    snapshot_service = SnapshotService(ledger)
    _emit(
        {
            "status": "ok",
            "config": {
                "version": config.config_version,
                "member_digest": config.member_digest,
                "config_digest": config.config_digest,
            },
            "recovery": session.report.to_dict(),
            "runtime": session.engine.snapshot_state().to_dict(),
            "rounds": ledger.round_history(limit=args.limit),
            "snapshots": snapshot_service.list(),
        }
    )
    return 0


def cmd_snapshot(args: argparse.Namespace) -> int:
    config, ledger, session, engine, _controller, _recovery = _load_runtime(
        args.config, args.db
    )
    service = SnapshotService(ledger)
    snapshot = service.create(
        config,
        engine,
        label=args.label,
        window=args.window,
    )
    _emit({"status": "ok", "snapshot": snapshot.summary()})
    return 0


def cmd_snapshots(args: argparse.Namespace) -> int:
    ledger = Ledger(args.db)
    _emit({"status": "ok", "snapshots": SnapshotService(ledger).list()})
    return 0


def cmd_restore_snapshot(args: argparse.Namespace) -> int:
    config = load_config(args.config)
    ledger = Ledger(args.db)
    service = SnapshotService(ledger)
    restore = service.restore(args.snapshot_id, config)
    _emit(restore.to_dict())
    if args.state_out:
        Path(args.state_out).write_text(
            json.dumps(restore.state.to_dict(), indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
    return 0


def cmd_replay(args: argparse.Namespace) -> int:
    config = load_config(args.config)
    ledger = Ledger(args.db)
    snapshot = SnapshotService(ledger).get(args.snapshot_id)
    report = ReplayVerifier(config).verify(snapshot)
    _emit(report.to_dict())
    return 0 if report.ok else 1


def cmd_ui(args: argparse.Namespace) -> int:
    from .ui import run_ui

    return run_ui(
        config_path=args.config,
        ledger_path=args.db,
        smoke=args.smoke,
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="lbhealth", description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)

    validate = subparsers.add_parser("validate", help="strictly validate a pool JSON file")
    validate.add_argument("--config", required=True)
    validate.set_defaults(func=cmd_validate)

    run = subparsers.add_parser("run", help="probe fixed rounds and persist the ledger")
    run.add_argument("--config", required=True)
    run.add_argument("--db", required=True)
    run.add_argument("--rounds", type=int, required=True)
    run.add_argument("--snapshot-every", type=int, default=0)
    run.add_argument("--snapshot-window", type=int, default=4)
    run.add_argument(
        "--no-fixed-period",
        action="store_true",
        help="do not pace round starts at the configured interval",
    )
    run.set_defaults(func=cmd_run)

    status = subparsers.add_parser("status", help="show recovered runtime, rounds, and snapshots")
    status.add_argument("--config", required=True)
    status.add_argument("--db", required=True)
    status.add_argument("--limit", type=int, default=100)
    status.set_defaults(func=cmd_status)

    snapshot = subparsers.add_parser("snapshot", help="create a versioned whole-state snapshot")
    snapshot.add_argument("--config", required=True)
    snapshot.add_argument("--db", required=True)
    snapshot.add_argument("--label", required=True)
    snapshot.add_argument("--window", type=int, default=4)
    snapshot.set_defaults(func=cmd_snapshot)

    snapshots = subparsers.add_parser("snapshots", help="list snapshots")
    snapshots.add_argument("--db", required=True)
    snapshots.set_defaults(func=cmd_snapshots)

    restore = subparsers.add_parser(
        "restore-snapshot", help="compute a whole-state replacement from a snapshot"
    )
    restore.add_argument("--config", required=True)
    restore.add_argument("--db", required=True)
    restore.add_argument("--snapshot-id", type=int, required=True)
    restore.add_argument("--state-out")
    restore.set_defaults(func=cmd_restore_snapshot)

    replay = subparsers.add_parser(
        "replay", help="re-probe the snapshot window and report the first divergence"
    )
    replay.add_argument("--config", required=True)
    replay.add_argument("--db", required=True)
    replay.add_argument("--snapshot-id", type=int, required=True)
    replay.set_defaults(func=cmd_replay)

    ui = subparsers.add_parser("ui", help="open the Tkinter application")
    ui.add_argument("--config", default=str(Path("config") / "sample_pool.json"))
    ui.add_argument("--db", default=str(Path("artifacts") / "ui_ledger.sqlite"))
    ui.add_argument("--smoke", action="store_true", help="open briefly and exit for environment checks")
    ui.set_defaults(func=cmd_ui)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if getattr(args, "rounds", 1) < 0:
        parser.error("--rounds must be non-negative")
    try:
        if getattr(args, "no_fixed_period", False):
            args.fixed_period = False
        else:
            args.fixed_period = True
        return int(args.func(args))
    except HealthError as exc:
        _emit(exc.to_dict())
        return 2
    except (OSError, ValueError) as exc:
        _emit(
            {
                "status": "error",
                "code": "E_RUNTIME",
                "message": str(exc),
            }
        )
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
