"""Command-line interface for load/merge/replay/patch/resume/status/verify."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Any

from .engine import (
    acknowledge,
    append_event,
    load_configuration,
    merge_ledger,
    patch_ledger,
    replay_ledger,
    status_payload,
)
from .errors import (
    LEDGER_NOT_INITIALIZED,
    SeqLedgerError,
    StoreError,
)
from .sample import generate_fixtures
from .store import LedgerStore
from .util import (
    pretty_json,
    read_json,
    read_jsonl,
    write_json_atomic,
)
from .validation import (
    validate_events,
    validate_logical_clock,
    validate_subscriptions,
    validate_topology,
    validate_v1,
    validate_v2,
)
from .verify import verify_ledger


def _add_db_option(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--db",
        default=argparse.SUPPRESS,
        help="SQLite ledger path (default: ledger.sqlite3)",
    )


def _add_out_option(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--out",
        help="Write the complete JSON result to this path as well as stdout",
    )


def _add_fault_options(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--fault-kill-after-commits",
        type=int,
        help="Test-only SIGKILL injection after N committed ledger versions",
    )
    parser.add_argument(
        "--fault-kill-before-commit",
        action="store_true",
        help="Test-only SIGKILL injection immediately before a ledger commit",
    )


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="seqledger",
        description="Deterministic two-generation sequential-delivery ledger tool",
    )
    _add_db_option(parser)
    subparsers = parser.add_subparsers(dest="command", required=True)

    load = subparsers.add_parser("load", help="Validate and append deterministic inputs")
    _add_db_option(load)
    _add_out_option(load)
    _add_fault_options(load)
    load.add_argument("--topology", required=True)
    load.add_argument("--events", required=True)
    load.add_argument("--subscriptions", required=True)
    load.add_argument("--expected-version", type=int)

    merge = subparsers.add_parser("merge", help="Merge v1 and v2 semantic snapshots")
    _add_db_option(merge)
    _add_out_option(merge)
    _add_fault_options(merge)
    merge.add_argument("--v1", required=True)
    merge.add_argument("--v2-manifest", required=True)
    merge.add_argument("--v2-meta", required=True)
    merge.add_argument("--logical-clock-ms", required=True, type=int)
    merge.add_argument("--expected-version", type=int)

    replay = subparsers.add_parser("replay", help="Advance sequential delivery to a logical clock")
    _add_db_option(replay)
    _add_out_option(replay)
    _add_fault_options(replay)
    replay.add_argument("--logical-clock-ms", required=True, type=int)
    replay.add_argument("--partition", action="append", dest="partitions")
    replay.add_argument("--max-steps", type=int)
    replay.add_argument("--expected-version", type=int)

    resume = subparsers.add_parser("resume", help="Recover and continue from the committed pointer")
    _add_db_option(resume)
    _add_out_option(resume)
    _add_fault_options(resume)
    resume.add_argument("--logical-clock-ms", required=True, type=int)
    resume.add_argument("--partition", action="append", dest="partitions")
    resume.add_argument("--max-steps", type=int)
    resume.add_argument("--expected-version", type=int)

    patch = subparsers.add_parser("patch", help="Apply a semantic patch and dependency closure")
    _add_db_option(patch)
    _add_out_option(patch)
    _add_fault_options(patch)
    patch.add_argument("--file", required=True, dest="patch_file")
    patch.add_argument("--expected-version", type=int)

    status = subparsers.add_parser("status", help="Emit stable partition status JSON")
    _add_db_option(status)
    _add_out_option(status)

    verify = subparsers.add_parser("verify", help="Independently replay and compare the ledger")
    _add_db_option(verify)
    _add_out_option(verify)
    verify.add_argument("--topology", required=True)
    verify.add_argument("--events", required=True)
    verify.add_argument("--subscriptions", required=True)
    verify.add_argument("--v1", required=True)
    verify.add_argument("--v2-manifest", required=True)
    verify.add_argument("--v2-meta", required=True)
    verify.add_argument("--logical-clock-ms", type=int)
    verify.add_argument("--merge-clock-ms", type=int)
    verify.add_argument("--patch", action="append", dest="patch_files")

    ack = subparsers.add_parser("ack", help="Idempotently confirm one in-flight delivery")
    _add_db_option(ack)
    _add_out_option(ack)
    ack.add_argument("--event-seq", required=True, type=int)
    ack.add_argument("--delivery-generation", required=True, type=int)
    ack.add_argument("--logical-clock-ms", required=True, type=int)

    generate = subparsers.add_parser("generate", help="Generate the fixed-seed fixture set")
    generate.add_argument("--out", required=True, dest="fixture_dir")
    return parser


def _db_path(args: argparse.Namespace) -> str:
    return str(getattr(args, "db", "ledger.sqlite3"))


def _require_existing_db(path: str) -> None:
    if not Path(path).exists():
        raise StoreError(
            LEDGER_NOT_INITIALIZED,
            "Ledger database does not exist; run load first",
            diagnostics={"db_path": path},
        )


def _store(args: argparse.Namespace) -> LedgerStore:
    return LedgerStore(
        _db_path(args),
        fault_kill_after_commits=getattr(args, "fault_kill_after_commits", None),
        fault_kill_before_commit=bool(
            getattr(args, "fault_kill_before_commit", False)
        ),
    )


def _emit(result: dict[str, Any], out_path: str | None = None) -> None:
    if out_path:
        write_json_atomic(out_path, result)
        result = dict(result)
        result["output_path"] = str(Path(out_path).resolve())
    sys.stdout.write(pretty_json(result))


def _validated_topology(path: str) -> dict[str, Any]:
    return validate_topology(read_json(path), source=path)


def _validated_bundle(
    args: argparse.Namespace,
) -> tuple[
    dict[str, Any],
    list[dict[str, Any]],
    list[dict[str, Any]],
]:
    topology = _validated_topology(args.topology)
    raw_events = read_jsonl(args.events)
    events = validate_events(raw_events, topology, source=args.events)
    subscriptions = validate_subscriptions(
        read_json(args.subscriptions),
        topology,
        source=args.subscriptions,
    )
    return topology, events, subscriptions


def _validated_semantics(
    v1_path: str,
    manifest_path: str,
    meta_path: str,
) -> tuple[dict[str, Any], dict[str, Any]]:
    v1 = validate_v1(read_json(v1_path), source=v1_path)
    v2 = validate_v2(
        read_json(manifest_path),
        manifest_path,
        meta_path=meta_path,
    )
    return v1, v2


def _command_load(args: argparse.Namespace) -> dict[str, Any]:
    topology, events, subscriptions = _validated_bundle(args)
    store = _store(args)
    initialization = store.initialize()
    state, metadata = store.read_state()
    expected_version = args.expected_version
    if expected_version is not None and expected_version != metadata.version:
        raise StoreError(
            "LEDGER_VERSION_REGRESSION",
            "Expected ledger version does not match the current pointer",
            diagnostics={
                "expected_version": expected_version,
                "current_version": metadata.version,
            },
        )
    config_result = store.commit(
        lambda working: load_configuration(
            working,
            topology=topology,
            subscriptions=subscriptions,
        ),
        expected_version=expected_version,
    )
    version = config_result.version
    appended = 0
    skipped = 0
    for event in events:
        result = store.commit(
            lambda working, event=event: append_event(working, event),
        )
        version = result.version
        if result.changed:
            appended += 1
        else:
            skipped += 1
    state_after, metadata_after = store.read_state()
    return {
        "ok": True,
        "schema": "seqledger-load-result/v1",
        "initialized": initialization.changed,
        "ledger_version": metadata_after.version,
        "ledger_sha256": metadata_after.sha256,
        "topics": [
            {
                "name": topic["name"],
                "partition_count": topic["partition_count"],
                "rule": topic["partition_key_rule"]["pattern"],
            }
            for topic in topology["topics"]
        ],
        "subscription_count": len(subscriptions),
        "event_count": len(events),
        "appended_events": appended,
        "idempotent_skips": skipped,
        "last_event_seq": state_after["last_event_seq"],
    }


def _command_merge(args: argparse.Namespace) -> dict[str, Any]:
    v1, v2 = _validated_semantics(args.v1, args.v2_manifest, args.v2_meta)
    _require_existing_db(_db_path(args))
    store = _store(args)
    result = merge_ledger(
        store,
        v1=v1,
        v2=v2,
        logical_clock_ms=args.logical_clock_ms,
        expected_version=args.expected_version,
    )
    return result


def _run_replay(args: argparse.Namespace, *, resume: bool) -> dict[str, Any]:
    _require_existing_db(_db_path(args))
    store = _store(args)
    inspection = store.inspect()
    result = replay_ledger(
        store,
        logical_clock_ms=args.logical_clock_ms,
        partition_filter=args.partitions,
        max_steps=args.max_steps,
        expected_version=args.expected_version,
    )
    if resume:
        result["schema"] = "seqledger-resume-result/v1"
        result["recovered_pointer_version"] = inspection["current_version"]
        result["ledger_versions_checked"] = inspection["version_count"]
        result["recovery"] = "continued from the single committed snapshot pointer"
    return result


def _command_patch(args: argparse.Namespace) -> dict[str, Any]:
    _require_existing_db(_db_path(args))
    patch_document = read_json(args.patch_file)
    store = _store(args)
    return patch_ledger(
        store,
        patch_document,
        expected_version=args.expected_version,
    )


def _command_status(args: argparse.Namespace) -> dict[str, Any]:
    _require_existing_db(_db_path(args))
    store = _store(args)
    state, metadata = store.read_state()
    return status_payload(state, metadata)


def _command_verify(args: argparse.Namespace) -> dict[str, Any]:
    _require_existing_db(_db_path(args))
    topology, events, subscriptions = _validated_bundle(args)
    v1, v2 = _validated_semantics(args.v1, args.v2_manifest, args.v2_meta)
    store = _store(args)
    state, _metadata = store.read_state()
    logical_clock = (
        args.logical_clock_ms
        if args.logical_clock_ms is not None
        else int(state["logical_clock_ms"])
    )
    merge_clock = args.merge_clock_ms
    if merge_clock is None:
        view = state.get("semantics", {}).get("view")
        if not view:
            raise StoreError(
                LEDGER_NOT_INITIALIZED,
                "Verification needs --merge-clock-ms when no unified view is present",
            )
        merge_clock = int(view["injected_logical_clock_ms"])
    validate_logical_clock(logical_clock)
    validate_logical_clock(merge_clock, path="merge_logical_clock_ms")
    patches = [
        read_json(path)
        for path in (args.patch_files or [])
    ]
    return verify_ledger(
        store,
        topology=topology,
        events=events,
        subscriptions=subscriptions,
        v1=v1,
        v2=v2,
        logical_clock_ms=int(logical_clock),
        merge_logical_clock_ms=int(merge_clock),
        patch_documents=patches,
    )


def _command_ack(args: argparse.Namespace) -> dict[str, Any]:
    _require_existing_db(_db_path(args))
    return acknowledge(
        _store(args),
        event_seq=args.event_seq,
        delivery_generation=args.delivery_generation,
        logical_clock_ms=args.logical_clock_ms,
    )


def main(argv: list[str] | None = None) -> int:
    parser = _parser()
    args = parser.parse_args(argv)
    try:
        if args.command == "generate":
            result = {
                "ok": True,
                "schema": "seqledger-generate-result/v1",
                "fixture_dir": str(Path(args.fixture_dir).resolve()),
                "fixture_manifest": generate_fixtures(args.fixture_dir),
            }
        elif args.command == "load":
            result = _command_load(args)
        elif args.command == "merge":
            result = _command_merge(args)
        elif args.command == "replay":
            result = _run_replay(args, resume=False)
        elif args.command == "resume":
            result = _run_replay(args, resume=True)
        elif args.command == "patch":
            result = _command_patch(args)
        elif args.command == "status":
            result = _command_status(args)
        elif args.command == "verify":
            result = _command_verify(args)
        elif args.command == "ack":
            result = _command_ack(args)
        else:
            parser.error(f"Unsupported command: {args.command}")
            return 2
        _emit(result, getattr(args, "out", None))
        return 0
    except SeqLedgerError as exc:
        sys.stdout.write(pretty_json(exc.to_dict()))
        return exc.exit_code
    except KeyboardInterrupt:
        sys.stdout.write(
            pretty_json(
                {
                    "ok": False,
                    "error": {
                        "code": "INTERRUPTED",
                        "message": "Interrupted by operator",
                        "diagnostics": {},
                        "ledger_written": False,
                    },
                }
            )
        )
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
