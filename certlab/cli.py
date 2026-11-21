from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from .clock import LogicalClock
from .engine import SampleRepository, build_and_validate
from .errors import CertLabError, ErrorCode
from .snapshot import SnapshotLoader, merge_snapshots
from .store import StateStore
from .util import load_json_file, stable_json


def _common(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--db", default=argparse.SUPPRESS, help="SQLite state database")
    parser.add_argument(
        "--clock-ms",
        type=int,
        default=argparse.SUPPRESS,
        help="injected logical clock in epoch milliseconds",
    )
    parser.add_argument(
        "--tolerance-ms",
        type=int,
        default=argparse.SUPPRESS,
        help="allowed logical-clock rollback tolerance (default: 5000)",
    )
    parser.add_argument(
        "--sample",
        default=argparse.SUPPRESS,
        help="local sample directory",
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="certlab")
    parser.add_argument("--db", default=".certlab/state.sqlite")
    parser.add_argument("--clock-ms", type=int, default=None)
    parser.add_argument("--tolerance-ms", type=int, default=5000)
    parser.add_argument("--sample", default=None)
    subparsers = parser.add_subparsers(dest="command", required=True)

    load_parser = subparsers.add_parser("load")
    _common(load_parser)
    load_parser.add_argument("--v1")
    load_parser.add_argument("--v2")

    chain_parser = subparsers.add_parser("chain")
    _common(chain_parser)
    chain_parser.add_argument("--leaf", required=True)
    chain_parser.add_argument("--hostname")
    chain_parser.add_argument("--purpose")

    verify_parser = subparsers.add_parser("verify")
    _common(verify_parser)
    verify_parser.add_argument("--leaf", action="append", default=[])
    verify_parser.add_argument("--hostname")
    verify_parser.add_argument("--purpose")

    apply_parser = subparsers.add_parser("apply")
    _common(apply_parser)
    apply_parser.add_argument("--patch", required=True)

    resume_parser = subparsers.add_parser("resume")
    _common(resume_parser)

    status_parser = subparsers.add_parser("status")
    _common(status_parser)

    audit_parser = subparsers.add_parser("verify-audit")
    _common(audit_parser)
    return parser


def _clock(args: argparse.Namespace) -> LogicalClock:
    if args.clock_ms is None:
        raise CertLabError(
            ErrorCode.INVALID_ARGUMENT,
            "--clock-ms is required; CertLab never reads the system wall clock",
        )
    return LogicalClock(args.clock_ms)


def _store(args: argparse.Namespace) -> StateStore:
    return StateStore(args.db, clock_tolerance_ms=args.tolerance_ms)


def _state_sample(
    store: StateStore, state: dict[str, Any], override: str | None
) -> str:
    return str(Path(override).resolve()) if override else state["sample_dir"]


def _leaf_requests(
    repository: SampleRepository,
    leaves: list[str],
    hostname: str | None,
    purpose: str | None,
) -> list[dict[str, str]]:
    selected = leaves or [str(item["id"]) for item in repository.leaf_index]
    if len(selected) > 1 and (hostname is not None or purpose is not None):
        raise CertLabError(
            ErrorCode.INVALID_ARGUMENT,
            "--hostname/--purpose are only valid with a single --leaf",
        )
    requests: list[dict[str, str]] = []
    for selected_leaf in selected:
        entry, _ = repository.load_leaf(selected_leaf)
        request_hostname = hostname or entry.get("hostname")
        request_purpose = purpose or entry.get("purpose")
        if not isinstance(request_hostname, str):
            raise CertLabError(
                ErrorCode.INVALID_ARGUMENT,
                "hostname is required for a leaf outside leaves/index.json",
                {"leaf_id": selected_leaf},
            )
        if not isinstance(request_purpose, str):
            raise CertLabError(
                ErrorCode.INVALID_ARGUMENT,
                "purpose is required for a leaf outside leaves/index.json",
                {"leaf_id": selected_leaf},
            )
        requests.append(
            {
                "leaf_id": selected_leaf,
                "hostname": request_hostname,
                "purpose": request_purpose,
            }
        )
    return requests


def command_load(args: argparse.Namespace) -> dict[str, Any]:
    clock = _clock(args)
    if not args.v1 and not args.v2:
        raise CertLabError(
            ErrorCode.INVALID_ARGUMENT, "load requires --v1 and/or --v2"
        )
    snapshots = SnapshotLoader.load(
        v1_path=args.v1, v2_path=args.v2, clock=clock
    )
    view = merge_snapshots(snapshots, clock)
    sample = args.sample
    if not sample:
        if args.v1:
            sample = str(Path(args.v1).resolve().parent)
        else:
            sample = str(Path(args.v2).resolve().parent)
    if not Path(sample).is_dir():
        raise CertLabError(
            ErrorCode.INVALID_ARGUMENT,
            "sample directory does not exist",
            {"sample": sample},
        )
    store = _store(args)
    result = store.initialize(view, sample_dir=sample, clock=clock)
    result["source_versions"] = [snapshot.version for snapshot in snapshots]
    result["merge_decisions"] = view["merge_decisions"]
    return result


def command_chain(args: argparse.Namespace) -> dict[str, Any]:
    clock = _clock(args)
    store = _store(args)
    state, view = store.load_view()
    sample = _state_sample(store, state, args.sample)
    repository = SampleRepository(sample, view)
    entry, leaf = repository.load_leaf(args.leaf)
    hostname = args.hostname or entry.get("hostname")
    purpose = args.purpose or entry.get("purpose")
    if not hostname or not purpose:
        raise CertLabError(
            ErrorCode.INVALID_ARGUMENT,
            "--hostname and --purpose are required for unresolved leaf metadata",
        )
    conclusion = build_and_validate(
        repository,
        leaf,
        hostname=str(hostname),
        purpose=str(purpose),
        now_ms=clock.now_ms,
    )
    return {
        "ok": True,
        "trust_version": state["version"],
        "trust_generation": state["generation"],
        "logical_clock_ms": clock.now_ms,
        "chain": conclusion,
    }


def command_verify(args: argparse.Namespace) -> dict[str, Any]:
    clock = _clock(args)
    store = _store(args)
    state, view = store.load_view()
    sample = _state_sample(store, state, args.sample)
    repository = SampleRepository(sample, view)
    requests = _leaf_requests(repository, args.leaf, args.hostname, args.purpose)
    return store.verify_requests(requests, clock=clock, sample_dir=sample)


def command_apply(args: argparse.Namespace) -> dict[str, Any]:
    clock = _clock(args)
    patch_path = Path(args.patch)
    try:
        patch = load_json_file(patch_path)
    except Exception as exc:
        raise CertLabError(
            ErrorCode.BAD_PATCH,
            "patch is not valid JSON",
            {"path": str(patch_path), "cause": type(exc).__name__},
        ) from exc
    store = _store(args)
    return store.apply_patch(patch, sample_dir=args.sample, clock=clock)


def command_resume(args: argparse.Namespace) -> dict[str, Any]:
    clock = _clock(args)
    return _store(args).resume(clock=clock)


def command_status(args: argparse.Namespace) -> dict[str, Any]:
    return _store(args).status()


def command_audit(args: argparse.Namespace) -> dict[str, Any]:
    return _store(args).verify_audit()


COMMANDS = {
    "load": command_load,
    "chain": command_chain,
    "verify": command_verify,
    "apply": command_apply,
    "resume": command_resume,
    "status": command_status,
    "verify-audit": command_audit,
}


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        result = COMMANDS[args.command](args)
    except CertLabError as exc:
        print(stable_json(exc.to_dict()))
        return 2
    except FileNotFoundError as exc:
        error = CertLabError(
            ErrorCode.INVALID_ARGUMENT,
            str(exc),
            {"exception_type": "FileNotFoundError"},
        )
        print(stable_json(error.to_dict()))
        return 2
    except Exception as exc:
        error = CertLabError(
            ErrorCode.INTERNAL,
            "unexpected internal error",
            {"exception_type": type(exc).__name__, "message": str(exc)},
        )
        print(stable_json(error.to_dict()))
        return 3
    print(stable_json(result))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

