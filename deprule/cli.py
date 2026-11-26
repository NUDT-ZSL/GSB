"""Command line entry for deterministic rule migration and parsing."""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

from .errors import Codes, DomainError
from .rules import migrate_snapshot, read_snapshot
from .seed import seed_workspace
from .store import VersionedStore
from .tasks import load_tasks
from .verify import adjudicate_equivalence, replay_export
from .runner import run_pipeline


def emit(payload: dict) -> int:
    print(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=False))
    return 0


def write_parse_outputs(out_dir: Path, result: dict, ledger: dict, ledger_record: dict | None) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "parse-result.json").write_text(
        json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    with (out_dir / "parse-results.jsonl").open("w", encoding="utf-8") as handle:
        for item in result["results"]:
            handle.write(json.dumps(item, ensure_ascii=False, sort_keys=True) + "\n")
    (out_dir / "invalidation-ledger.json").write_text(
        json.dumps(ledger, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    if ledger_record is not None:
        (out_dir / "ledger-versioned-record.json").write_text(
            json.dumps(ledger_record, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )


def cmd_seed(args) -> dict:
    report = seed_workspace(args.out, force=not args.no_force)
    return {"ok": True, "code": Codes.OK, "seed": report}


def cmd_read_rule(args) -> dict:
    snapshot = read_snapshot(args.rules)
    return {
        "ok": True,
        "code": Codes.OK,
        "format": snapshot["format"],
        "source_hash": snapshot["source_hash"],
        "semantic_hash": snapshot["ir"]["semantic_hash"],
        "rule_revision": snapshot["ir"]["rule_revision"],
        "tag_set_version": snapshot["ir"]["tag_set_version"],
        "root_policy_version": snapshot["ir"]["root_policy_version"],
    }


def cmd_migrate(args) -> dict:
    report = migrate_snapshot(
        args.source,
        args.to,
        args.out,
        downgrade=args.policy,
        verify=not args.no_verify,
    )
    return {"ok": True, "code": Codes.OK, "migration": report}


def _load_previous_ledger(args) -> dict | None:
    if args.previous_ledger:
        path = Path(args.previous_ledger)
        return json.loads(path.read_text(encoding="utf-8"))
    if args.incremental:
        head = Path(args.state_dir) / "ledgers" / "head.json"
        if head.exists():
            record_path = Path(args.state_dir) / "ledgers" / "versions" / json.loads(
                head.read_text(encoding="utf-8")
            )["document"]
            return json.loads(record_path.read_text(encoding="utf-8"))["document"]
    return None


def cmd_parse(args) -> dict:
    tasks = load_tasks(args.tasks)
    snapshot = read_snapshot(args.rules)
    old_snapshot = read_snapshot(args.old_rules) if args.old_rules else None
    previous_ledger = _load_previous_ledger(args)

    result, ledger = run_pipeline(
        tasks,
        snapshot,
        args.state_dir,
        previous_ledger=previous_ledger,
        old_snapshot=old_snapshot,
        resume=not args.no_resume,
        fresh=args.fresh,
        crash_mid_stage=args.crash_mid_stage,
        crash_after_stage=args.crash_after_stage,
    )

    ledger_record = None
    if not args.no_commit_ledger:
        store = VersionedStore(args.state_dir, "ledgers")
        ledger_record = store.commit(ledger, incoming_revision=int(snapshot["ir"]["rule_revision"]))
        ledger["store_version"] = ledger_record["version"]
        ledger["store_checksum"] = ledger_record["checksum"]

    out_dir = Path(args.out_dir)
    write_parse_outputs(out_dir, result, ledger, ledger_record)
    return {
        "ok": True,
        "code": Codes.OK,
        "out_dir": str(out_dir),
        "result_file": str(out_dir / "parse-result.json"),
        "jsonl_file": str(out_dir / "parse-results.jsonl"),
        "ledger_file": str(out_dir / "invalidation-ledger.json"),
        "sentence_count": len(tasks),
        "affected_sentence_ids": ledger["affected_sentence_ids"],
        "linguistic_result_changed_sentence_ids": ledger["linguistic_result_changed_sentence_ids"],
        "recomputed_node_instances": ledger["recomputed_node_instances"],
        "reused_node_instances": ledger["reused_node_instances"],
        "recovered_node_instances": ledger["recovered_node_instances"],
        "recomputed_kinds": ledger["recomputed_kinds"],
        "reused_kinds": ledger["reused_kinds"],
    }


def cmd_verify_equivalence(args) -> dict:
    tasks = load_tasks(args.tasks)
    snapshots = {
        "v1": read_snapshot(args.v1),
        "v2": read_snapshot(args.v2),
        "v3": read_snapshot(args.v3),
    }
    report = adjudicate_equivalence(tasks, snapshots, args.state_dir)
    if args.out:
        Path(args.out).parent.mkdir(parents=True, exist_ok=True)
        Path(args.out).write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return {"ok": True, "code": Codes.OK, **report}


def cmd_replay(args) -> dict:
    export_doc = json.loads(Path(args.export).read_text(encoding="utf-8"))
    tasks = load_tasks(args.tasks)
    snapshot = read_snapshot(args.rules)
    report = replay_export(export_doc, tasks, snapshot, args.state_dir)
    return {"ok": True, "code": Codes.OK, **report}


def cmd_commit_rules(args) -> dict:
    snapshot = read_snapshot(args.rules)
    store = VersionedStore(args.state_dir, "rules")
    document = {
        "snapshot_format": snapshot["format"],
        "source_hash": snapshot["source_hash"],
        "ir": snapshot["ir"],
    }
    record = store.commit(document, incoming_revision=int(snapshot["ir"]["rule_revision"]))
    return {
        "ok": True,
        "code": Codes.OK,
        "domain": "rules",
        "store_version": record["version"],
        "revision": record["revision"],
        "checksum": record["checksum"],
    }


def cmd_lock_hold(args) -> dict:
    store = VersionedStore(args.state_dir, args.domain)
    lock = store.acquire_hold_for_demo()
    print(json.dumps({"ok": True, "code": Codes.OK, "status": "READY", "domain": args.domain}), flush=True)
    try:
        sys.stdin.readline()
    finally:
        lock.release()
    return {"ok": True, "code": Codes.OK, "status": "RELEASED"}


def cmd_demo_concurrency(args) -> dict:
    state = Path(args.state_dir)
    state.mkdir(parents=True, exist_ok=True)
    holder = subprocess.Popen(
        [sys.executable, "-m", "deprule.cli", "lock-hold", "--state-dir", str(state), "--domain", "rules"],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    ready_line = holder.stdout.readline().strip()
    if not ready_line:
        stderr = holder.stderr.read() if holder.stderr else ""
        raise DomainError("lock holder failed to start", code=Codes.E_INTERNAL, stderr=stderr)

    busy = subprocess.run(
        [
            sys.executable, "-m", "deprule.cli", "commit-rules",
            "--state-dir", str(state), "--rules", args.rules,
        ],
        text=True,
        capture_output=True,
        check=False,
    )
    try:
        busy_payload = json.loads(busy.stdout)
    except json.JSONDecodeError:
        busy_payload = {"raw_stdout": busy.stdout, "stderr": busy.stderr, "returncode": busy.returncode}

    holder.stdin.write("release\n")
    holder.stdin.flush()
    holder.wait(timeout=5)
    return {
        "ok": busy_payload.get("code") == Codes.E_STAGE_BUSY,
        "code": busy_payload.get("code", Codes.E_INTERNAL),
        "message": "late concurrent committer received non-blocking E_STAGE_BUSY",
        "holder_ready": json.loads(ready_line),
        "busy_commit": busy_payload,
        "holder_exit_code": holder.returncode,
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="deprule", description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    seed = sub.add_parser("seed", help="generate deterministic local inputs")
    seed.add_argument("--out", default="data")
    seed.add_argument("--no-force", action="store_true")
    seed.set_defaults(func=cmd_seed)

    read_rule = sub.add_parser("read-rule", help="read and validate one rule snapshot")
    read_rule.add_argument("--rules", required=True)
    read_rule.set_defaults(func=cmd_read_rule)

    migrate = sub.add_parser("migrate", help="migrate a rule snapshot to v1/v2/v3/ir")
    migrate.add_argument("--source", required=True)
    migrate.add_argument("--to", required=True, choices=["v1", "v2", "v3", "ir"])
    migrate.add_argument("--out", required=True)
    migrate.add_argument("--policy", default="flatten", choices=["flatten", "discard"])
    migrate.add_argument("--no-verify", action="store_true")
    migrate.set_defaults(func=cmd_migrate)

    parse = sub.add_parser("parse", help="run deterministic parsing DAG")
    parse.add_argument("--tasks", required=True)
    parse.add_argument("--rules", required=True)
    parse.add_argument("--state-dir", default="artifacts/state")
    parse.add_argument("--out-dir", default="artifacts/runs/latest")
    parse.add_argument("--old-rules")
    parse.add_argument("--previous-ledger")
    parse.add_argument("--incremental", action="store_true")
    parse.add_argument("--fresh", action="store_true")
    parse.add_argument("--no-resume", action="store_true")
    parse.add_argument("--no-commit-ledger", action="store_true")
    parse.add_argument("--crash-mid-stage", type=int, choices=[1, 2, 3, 4])
    parse.add_argument("--crash-after-stage", type=int, choices=[1, 2, 3, 4])
    parse.set_defaults(func=cmd_parse)

    equiv = sub.add_parser("verify-equivalence", help="adjudicate v1/v2/v3 semantic and parse equivalence")
    equiv.add_argument("--tasks", required=True)
    equiv.add_argument("--v1", required=True)
    equiv.add_argument("--v2", required=True)
    equiv.add_argument("--v3", required=True)
    equiv.add_argument("--state-dir", default="artifacts/state")
    equiv.add_argument("--out")
    equiv.set_defaults(func=cmd_verify_equivalence)

    replay = sub.add_parser("replay", help="replay an export and detect stale cache versions")
    replay.add_argument("--export", required=True)
    replay.add_argument("--tasks", required=True)
    replay.add_argument("--rules", required=True)
    replay.add_argument("--state-dir", default="artifacts/state")
    replay.set_defaults(func=cmd_replay)

    commit = sub.add_parser("commit-rules", help="commit a rule snapshot to the versioned head store")
    commit.add_argument("--state-dir", default="artifacts/state")
    commit.add_argument("--rules", required=True)
    commit.set_defaults(func=cmd_commit_rules)

    hold = sub.add_parser("lock-hold", help="test helper: hold commit lock until stdin line")
    hold.add_argument("--state-dir", default="artifacts/state")
    hold.add_argument("--domain", default="rules", choices=["rules", "ledgers"])
    hold.set_defaults(func=cmd_lock_hold)

    busy = sub.add_parser("demo-concurrency", help="demonstrate immediate non-blocking busy verdict")
    busy.add_argument("--state-dir", default="artifacts/concurrency-state")
    busy.add_argument("--rules", required=True)
    busy.set_defaults(func=cmd_demo_concurrency)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        payload = args.func(args)
    except DomainError as exc:
        payload = exc.to_envelope()
    except Exception as exc:  # noqa: BLE001 - CLI boundary returns business envelope
        payload = {
            "ok": False,
            "code": Codes.E_INTERNAL,
            "message": str(exc),
            "exception_type": type(exc).__name__,
        }
    emit(payload)
    if payload.get("ok"):
        return 0
    return 1 if payload.get("code") == Codes.E_INTERNAL else 2


if __name__ == "__main__":
    raise SystemExit(main())
