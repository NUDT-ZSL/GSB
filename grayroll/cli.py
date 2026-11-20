from __future__ import annotations

import argparse
import fcntl
import json
import sys
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from .engine import Engine
from .errors import GrayrollError, exit_code_for
from .events import load_events, normalize_event
from .samples import SERVICE
from .sources import load_package
from .store import Store
from .util import canonical_bytes, canonical_json, sha256_bytes
from .verify import run_verification

PROJECT_ROOT = Path(__file__).resolve().parents[1]


def _open_existing(path: Path) -> Store:
    if not path.is_file():
        raise GrayrollError("E_NO_STATE", f"state database not found: {path}")
    return Store(path)


@contextmanager
def _state_lock(path: Path) -> Iterator[None]:
    path.parent.mkdir(parents=True, exist_ok=True)
    lock_path = path.with_name(path.name + ".lock")
    with lock_path.open("a+b") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def _event_id(prefix: str, payload: dict[str, Any]) -> str:
    return f"{prefix}-{sha256_bytes(canonical_bytes(payload))[:16]}"


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="grayroll")
    parser.add_argument(
        "--state",
        type=Path,
        default=Path("runs/state.db"),
        help="SQLite state database",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    load = subparsers.add_parser("load", help="validate and stage a rhythm package")
    load.add_argument("--manifest", type=Path, required=True)
    load.add_argument("--defaults", type=Path, required=True)

    merge = subparsers.add_parser("merge", help="merge v1/v2 and activate a package")
    merge.add_argument("--package-id")

    for name in ("ramp", "resume"):
        command = subparsers.add_parser(name)
        command.add_argument("--until", type=int, required=True)
        command.add_argument("--events", type=Path)

    observe = subparsers.add_parser("observe", help="inject one metric snapshot")
    observe.add_argument("--until", type=int, required=True)
    observe.add_argument("--batch-id", required=True)
    observe.add_argument("--tier-ratio", type=float, required=True)
    observe.add_argument("--sample-count", type=int, required=True)
    observe.add_argument("--metrics", required=True)
    observe.add_argument("--service", default=SERVICE)
    observe.add_argument("--event-id")

    rollback = subparsers.add_parser("rollback", help="inject a manual abort")
    rollback.add_argument("--until", type=int, required=True)
    rollback.add_argument("--batch-id", required=True)
    rollback.add_argument("--reason", default="manual_abort")
    rollback.add_argument("--service", default=SERVICE)
    rollback.add_argument("--event-id")

    subparsers.add_parser("status", help="emit stable JSON status")

    verify = subparsers.add_parser("verify", help="run fixed-sample determinism checks")
    verify.add_argument("--samples", type=Path, default=PROJECT_ROOT / "samples")
    verify.add_argument(
        "--workdir", type=Path, default=PROJECT_ROOT / "runs" / "verify"
    )
    return parser


def _print(value: Any) -> None:
    sys.stdout.write(canonical_json(value) + "\n")


def dispatch(arguments: argparse.Namespace) -> dict[str, Any]:
    state_path: Path = arguments.state
    if arguments.command == "load":
        package = load_package(arguments.manifest, arguments.defaults)
        with _state_lock(state_path):
            store = Store(state_path)
            try:
                return store.load_package(package)
            finally:
                store.close()
    if arguments.command == "merge":
        with _state_lock(state_path):
            store = _open_existing(state_path)
            try:
                package_id = arguments.package_id or store.latest_staged_package()
                return store.merge_package(package_id)
            finally:
                store.close()
    if arguments.command in {"ramp", "resume"}:
        events = load_events(arguments.events) if arguments.events else []
        with _state_lock(state_path):
            store = _open_existing(state_path)
            try:
                engine = Engine(store)
                if arguments.command == "resume":
                    return engine.resume(arguments.until, events)
                return engine.advance(arguments.until, events)
            finally:
                store.close()
    if arguments.command == "observe":
        try:
            metrics = json.loads(arguments.metrics)
        except json.JSONDecodeError as exc:
            raise GrayrollError("E_BAD_EVENT", f"invalid metrics JSON: {exc}") from exc
        if not isinstance(metrics, dict):
            raise GrayrollError("E_BAD_EVENT", "metrics must be a JSON object")
        event = {
            "batch_id": arguments.batch_id,
            "logical_ms": arguments.until,
            "metrics": metrics,
            "sample_count": arguments.sample_count,
            "service": arguments.service,
            "tier_ratio": arguments.tier_ratio,
            "type": "snapshot",
        }
        event["event_id"] = arguments.event_id or _event_id("obs-cli", event)
        event = normalize_event(event)
        with _state_lock(state_path):
            store = _open_existing(state_path)
            try:
                return Engine(store).advance(arguments.until, [event])
            finally:
                store.close()
    if arguments.command == "rollback":
        event = {
            "batch_id": arguments.batch_id,
            "logical_ms": arguments.until,
            "reason": arguments.reason,
            "service": arguments.service,
            "type": "manual_abort",
        }
        event["event_id"] = arguments.event_id or _event_id("abort-cli", event)
        event = normalize_event(event)
        with _state_lock(state_path):
            store = _open_existing(state_path)
            try:
                return Engine(store).advance(arguments.until, [event])
            finally:
                store.close()
    if arguments.command == "status":
        store = _open_existing(state_path)
        try:
            return store.status()
        finally:
            store.close()
    if arguments.command == "verify":
        return run_verification(
            PROJECT_ROOT,
            arguments.samples.resolve(),
            arguments.workdir.resolve(),
        )
    raise GrayrollError("E_INVALID_STATE", f"unknown command: {arguments.command}")


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    try:
        arguments = parser.parse_args(argv)
        result = dispatch(arguments)
        _print(result)
        return 0
    except GrayrollError as exc:
        _print(exc.as_dict())
        return exit_code_for(exc.code)
    except BrokenPipeError:
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
