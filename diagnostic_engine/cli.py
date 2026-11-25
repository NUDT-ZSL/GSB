"""Command line entry point for local deterministic diagnostics."""

from __future__ import annotations

import argparse
import json
import signal
import sys
import time
from pathlib import Path
from typing import Any

from .engine import DiagnosticEngine, engine_from_files
from .errors import DiagnosticError
from .generator import generate_fixture
from .hashing import canonical_json
from .state import StateManager


def _print(payload: dict[str, Any] | list[Any]) -> None:
    print(canonical_json(payload))


def _load_json(path: str | Path) -> dict[str, Any]:
    return json.loads(Path(path).read_text(encoding="utf-8"))


def _install_signal_reporting(engine: DiagnosticEngine) -> None:
    def handler(signum: int, _frame: Any) -> None:
        latest = []
        for path in engine.state.all_snapshot_files():
            snapshot = json.loads(path.read_text(encoding="utf-8"))
            latest.append(
                {
                    "window_id": snapshot.get("window_id"),
                    "profile": snapshot.get("precision_profile"),
                    "stage_index": snapshot.get("stage_index"),
                    "version": snapshot.get("version"),
                    "completed_v_idx_count": snapshot.get("completed_v_idx_count"),
                }
            )
        report_path = engine.state.write_exit_report(
            {
                "ok": False,
                "exit_reason": "catchable_signal",
                "signal": signum,
                "committed_snapshots": latest,
                "recovery_promise": "restart reuses atomically committed stages only",
            }
        )
        _print({"ok": False, "error_code": "E_SIGNAL_EXIT", "signal": signum, "exit_report": str(report_path)})
        raise SystemExit(128 + signum)

    signal.signal(signal.SIGTERM, handler)
    signal.signal(signal.SIGINT, handler)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="diagnostic-engine")
    sub = parser.add_subparsers(dest="command", required=True)

    generate = sub.add_parser("generate", help="generate deterministic sample JSONL and scenario JSON")
    generate.add_argument("--out", default="samples")
    generate.add_argument("--seed", type=int, default=440)

    run = sub.add_parser("run", help="run all windows")
    run.add_argument("--config", default="samples/scenario.json")
    run.add_argument("--observations", default="samples/observations.jsonl")
    run.add_argument("--profile", default=None)
    run.add_argument("--workdir", default=".")
    run.add_argument("--sigkill-after-commit", type=int, default=None)

    query = sub.add_parser("query", help="query a completed v_idx trace")
    query.add_argument("v_idx", type=int)
    query.add_argument("--config", default="samples/scenario.json")
    query.add_argument("--observations", default="samples/observations.jsonl")
    query.add_argument("--profile", default=None)
    query.add_argument("--workdir", default=".")

    verify = sub.add_parser("verify", help="replay and verify a v_idx result")
    verify.add_argument("v_idx", type=int)
    verify.add_argument("--config", default="samples/scenario.json")
    verify.add_argument("--observations", default="samples/observations.jsonl")
    verify.add_argument("--profile", default=None)
    verify.add_argument("--workdir", default=".")

    hold = sub.add_parser("hold-commit-lock", help="test helper: hold the global commit lock")
    hold.add_argument("--workdir", default=".")
    hold.add_argument("--seconds", type=float, default=10.0)

    wait_signal = sub.add_parser("wait-signal", help="test helper: install reporting and wait for a signal")
    wait_signal.add_argument("--workdir", default=".")
    wait_signal.add_argument("--seconds", type=float, default=30.0)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        if args.command == "generate":
            paths = generate_fixture(args.out, seed=args.seed)
            _print({"ok": True, "paths": {k: str(v) for k, v in paths.items()}})
            return 0

        if args.command == "hold-commit-lock":
            from .cache import CommitLock

            state = StateManager(Path(args.workdir) / ".diagnostic" / "state")
            with CommitLock(state.lock_path):
                print("LOCK_READY", flush=True)
                time.sleep(args.seconds)
            return 0

        if args.command == "wait-signal":
            state = StateManager(Path(args.workdir) / ".diagnostic" / "state")

            def handle_signal(signum: int, _frame: Any) -> None:
                path = state.write_exit_report(
                    {"ok": False, "exit_reason": "catchable_signal", "signal": signum}
                )
                _print({"ok": False, "error_code": "E_SIGNAL_EXIT", "exit_report": str(path)})
                raise SystemExit(128 + signum)

            signal.signal(signal.SIGTERM, handle_signal)
            signal.signal(signal.SIGINT, handle_signal)
            print("WAIT_READY", flush=True)
            time.sleep(args.seconds)
            return 0

        engine = engine_from_files(args.config, args.observations, args.profile, args.workdir)
        if args.command == "run":
            engine.crash_after_commits = args.sigkill_after_commit
            _install_signal_reporting(engine)
            report = engine.run_all()
            _print(report)
            return 0
        if args.command == "query":
            _print({"ok": True, "result": engine.query_v_idx(args.v_idx, args.profile)})
            return 0
        if args.command == "verify":
            _print(engine.verify_v_idx(args.v_idx, args.profile))
            return 0
        parser.error(f"unknown command: {args.command}")
        return 2
    except DiagnosticError as exc:
        _print(exc.to_dict())
        return 2
    except FileNotFoundError as exc:
        _print({"ok": False, "error_code": "E_NOT_FOUND", "error": str(exc)})
        return 2


if __name__ == "__main__":
    sys.exit(main())
