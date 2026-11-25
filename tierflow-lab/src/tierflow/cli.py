from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from .canonical import canonical_dumps, sha256_json
from .diagnostics import verify_project
from .errors import TierFlowError
from .policy import load_v1_policy, load_v2_policy, merge_policies
from .scheduler import adjudicate, recover_interrupted, round_report, run_migration
from .storage import StateStore, object_set_digest
from .validation import validate_config, validate_events, validate_manifest


def _store(args: argparse.Namespace) -> StateStore:
    return StateStore(args.db)


def _default_clock(args: argparse.Namespace) -> int:
    if args.clock is not None:
        return int(args.clock)
    config_path = Path(args.samples) / "config.json"
    if config_path.exists():
        return int(validate_config(config_path)["reference_clock_ms"])
    return 0


def cmd_load(args: argparse.Namespace) -> dict[str, Any]:
    samples = Path(args.samples)
    manifest = validate_manifest(samples / "manifest.json")
    config = validate_config(samples / "config.json")
    events = validate_events(manifest, samples / "migration_events.json")
    clock = _default_clock(args)
    store = _store(args)
    result = store.initialize_bundle(
        manifest,
        config,
        events,
        clock_ms=clock,
        reset=bool(args.reset),
    )
    accounts = store.accounts()
    store.close()
    return {
        "ok": True,
        "command": "load",
        "reset": bool(args.reset),
        "db_path": str(args.db),
        "object_count": manifest["object_count"],
        "event_count": events["event_count"],
        "object_set_digest": result["object_set_digest"],
        "clock_watermark_ms": result["clock_watermark_ms"],
        "accounts": accounts,
    }


def _apply_update_package(store: StateStore, args: argparse.Namespace, clock: int) -> dict[str, Any]:
    from .canonical import load_json

    update = load_json(args.update_package)
    current = store.current_policy()
    if current is None:
        raise TierFlowError("no current policy snapshot; run merge before applying an update")
    current_package = json.loads(current["package_json"])
    entries: dict[str, Any] = dict(current_package.get("entries", {}))
    if update.get("update_mode") == "full":
        entries = dict(update.get("entries", {}))
    else:
        entries.update(update.get("entries", {}))
    tombstones = dict(current_package.get("tombstones", {}))
    digest = store.get_meta("object_set_digest")
    package = {
        "manifest_version": int(update["manifest_version"]),
        "generation": int(update["generation"]),
        "generation_floor": int(update.get("generation_floor", update["generation"])),
        "package_version": update.get("package_version", f"update-v{update['manifest_version']}"),
        "shard_hashes": dict(current_package.get("shard_hashes", {})),
        "object_set_digest": digest,
        "entries": entries,
        "tombstones": tombstones,
    }
    package["snapshot_digest"] = sha256_json(
        {
            "manifest_version": package["manifest_version"],
            "generation": package["generation"],
            "object_set_digest": digest,
            "entries": entries,
            "tombstones": tombstones,
        }
    )
    applied = store.apply_policy_package(
        package,
        applied_clock_ms=clock,
        update_kind=str(update.get("update_mode", "incremental")),
    )
    return {"ok": True, "command": "merge", "update_package": str(args.update_package), "apply": applied}


def cmd_merge(args: argparse.Namespace) -> dict[str, Any]:
    samples = Path(args.samples)
    clock = _default_clock(args)
    store = _store(args)
    store.init_schema()
    try:
        if args.update_package:
            output = _apply_update_package(store, args, clock)
            output["clock_ms"] = clock
            return output

        manifest = validate_manifest(samples / "manifest.json")
        v1 = load_v1_policy(samples / "policies" / "policy-v1.json", clock)
        v2 = load_v2_policy(
            samples / "policies" / "v2",
            samples / "policies" / "v2" / "policy-v2-meta.sqlite",
        )
        digest = object_set_digest(manifest["objects"])
        package = merge_policies(v1, v2, object_set_digest=digest)
        applied = store.apply_policy_package(package, applied_clock_ms=clock, update_kind="auto")
        return {
            "ok": True,
            "command": "merge",
            "clock_ms": clock,
            "apply": applied,
            "summary": package["summary"],
            "adjudications": package["adjudications"],
            "merged_entries": package["entries"],
            "tombstones": package["tombstones"],
            "snapshot_digest": package["snapshot_digest"],
        }
    finally:
        store.close()


def cmd_judge(args: argparse.Namespace) -> dict[str, Any]:
    store = _store(args)
    store.init_schema()
    clock = _default_clock(args)
    result = adjudicate(store, clock)
    with store.write() as conn:
        store.advance_clock(conn, clock)
    store.close()
    candidates = [item for item in result["judgements"] if item["migration_required"]]
    return {
        "ok": True,
        "command": "judge",
        "clock_ms": clock,
        "clock_check": result["clock_check"],
        "heat_config": result["heat_config"],
        "candidate_count": len(candidates),
        "judgements": result["judgements"],
    }


def cmd_migrate(args: argparse.Namespace) -> dict[str, Any]:
    clock = _default_clock(args)
    store = _store(args)
    report = run_migration(
        store,
        clock,
        args.round_id,
        budget_bytes=args.budget_bytes,
        concurrency=args.workers,
        task_delay_seconds=args.task_delay,
        fail_objects=set(args.fail_object or []),
        crash_after_plan=bool(args.crash_after_plan),
        crash_after_claim_object=args.crash_after_claim,
        crash_after_commit_object=args.crash_after_commit,
        crash_after_prepare_object=args.crash_after_prepare,
        max_launch=args.max_launch,
    )
    store.close()
    return {"ok": True, "command": "migrate", "clock_ms": clock, **report}


def cmd_resume(args: argparse.Namespace) -> dict[str, Any]:
    clock = _default_clock(args)
    store = _store(args)
    result = recover_interrupted(
        store,
        clock,
        args.round_id,
        force=bool(args.force),
        task_delay_seconds=args.task_delay,
        fail_objects=set(args.fail_object or []),
        retry_failed=bool(args.retry_failed),
    )
    store.close()
    return {"ok": True, "command": "resume", "clock_ms": clock, **result}


def cmd_status(args: argparse.Namespace) -> dict[str, Any]:
    store = _store(args)
    store.init_schema()
    policy = store.current_policy()
    rounds = [dict(row) for row in store.conn.execute("SELECT * FROM rounds ORDER BY round_id")]
    job_counts = {
        row["status"]: int(row["count"])
        for row in store.conn.execute("SELECT status, COUNT(*) AS count FROM migration_jobs GROUP BY status")
    }
    result: dict[str, Any] = {
        "ok": True,
        "command": "status",
        "db_path": str(args.db),
        "policy_pointer": None
        if policy is None
        else {
            "manifest_version": policy["manifest_version"],
            "generation": policy["generation"],
            "decision": policy["decision"],
            "snapshot_digest": policy["snapshot_digest"],
        },
        "accounts": store.accounts(),
        "watermarks": store.watermarks(),
        "clock_watermark_ms": store.get_clock_watermark()[0],
        "rounds": rounds,
        "job_status_counts": dict(sorted(job_counts.items())),
    }
    if args.round_id:
        result["round_report"] = round_report(store, args.round_id)
    elif rounds:
        result["latest_round_report"] = round_report(store, rounds[-1]["round_id"])
    store.close()
    return result


def cmd_verify(args: argparse.Namespace) -> dict[str, Any]:
    report = verify_project(args.samples, args.db)
    return {"command": "verify", **report}


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="tierflow", description="Hot/cold tier migration arbitrator")
    parser.add_argument("--samples", default="samples")
    parser.add_argument("--db", default="state/tierflow.db")
    parser.add_argument("--clock", type=int, default=None)
    subparsers = parser.add_subparsers(dest="subcommand", required=True)

    load_parser = subparsers.add_parser("load")
    load_parser.add_argument("--reset", action="store_true")
    load_parser.set_defaults(func=cmd_load)

    merge_parser = subparsers.add_parser("merge")
    merge_parser.add_argument("--update-package", default=None)
    merge_parser.set_defaults(func=cmd_merge)

    judge_parser = subparsers.add_parser("judge")
    judge_parser.set_defaults(func=cmd_judge)

    migrate_parser = subparsers.add_parser("migrate")
    migrate_parser.add_argument("--round-id", default="round-0001")
    migrate_parser.add_argument("--workers", type=int, default=None)
    migrate_parser.add_argument("--budget-bytes", type=int, default=None)
    migrate_parser.add_argument("--task-delay", type=float, default=0.0)
    migrate_parser.add_argument("--fail-object", action="append", default=[])
    migrate_parser.add_argument("--max-launch", type=int, default=None)
    migrate_parser.add_argument("--crash-after-plan", action="store_true", help=argparse.SUPPRESS)
    migrate_parser.add_argument("--crash-after-claim", default=None, help=argparse.SUPPRESS)
    migrate_parser.add_argument("--crash-after-commit", default=None, help=argparse.SUPPRESS)
    migrate_parser.add_argument("--crash-after-prepare", default=None, help=argparse.SUPPRESS)
    migrate_parser.set_defaults(func=cmd_migrate)

    resume_parser = subparsers.add_parser("resume")
    resume_parser.add_argument("--round-id", default=None)
    resume_parser.add_argument("--force", action="store_true")
    resume_parser.add_argument("--retry-failed", action="store_true")
    resume_parser.add_argument("--task-delay", type=float, default=0.0)
    resume_parser.add_argument("--fail-object", action="append", default=[])
    resume_parser.set_defaults(func=cmd_resume)

    status_parser = subparsers.add_parser("status")
    status_parser.add_argument("--round-id", default=None)
    status_parser.set_defaults(func=cmd_status)

    verify_parser = subparsers.add_parser("verify")
    verify_parser.set_defaults(func=cmd_verify)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        result = args.func(args)
    except TierFlowError as exc:
        print(canonical_dumps(exc.to_dict()))
        return 2
    except (OSError, ValueError, KeyError, json.JSONDecodeError) as exc:
        error = TierFlowError(f"{type(exc).__name__}: {exc}")
        print(canonical_dumps(error.to_dict()))
        return 1
    print(canonical_dumps(result))
    return 0


if __name__ == "__main__":
    sys.exit(main())
