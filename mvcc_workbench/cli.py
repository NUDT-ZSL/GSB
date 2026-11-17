"""Command-line interface for deterministic local acceptance runs."""

from __future__ import annotations

import argparse
import shutil
import time
import traceback
from pathlib import Path
from typing import Any, Sequence

from .background import BackgroundCommitter
from .engine import Workbench
from .errors import BusinessError
from .inject import inject_snapshot, run_anomaly_demo
from .logging_utils import StructuredLogger
from .script import ScriptRunner
from .util import atomic_write_json, pretty_json


SAMPLE_PATH = Path(__file__).resolve().parent.parent / "samples" / "deterministic_session.json"


def _emit(value: Any) -> None:
    print(pretty_json(value))


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m mvcc_workbench",
        description="Local SQLite MVCC visibility workbench",
    )
    parser.add_argument("--session-dir", default="run", help="session directory (default: run)")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("reset", help="delete the session directory for a reproducible run")
    sub.add_parser("init", help="initialise a session database")

    run_script = sub.add_parser("run-script", help="execute a deterministic JSON transaction script")
    run_script.add_argument("--script", default=str(SAMPLE_PATH))
    run_script.add_argument("--reset", action="store_true")
    run_script.add_argument("--budget-ms", type=float, default=5.0)
    run_script.add_argument("--snapshot", help="save a named snapshot after the script")

    for name, help_text in (("query", "run the interactive fast path"), ("verify", "compare fast path with authoritative replay")):
        command = sub.add_parser(name, help=help_text)
        scope = command.add_mutually_exclusive_group(required=True)
        scope.add_argument("--txid", type=int)
        scope.add_argument("--latest", action="store_true")
        command.add_argument("--table", required=True)
        command.add_argument("--key", required=True)
        command.add_argument("--budget-ms", type=float, default=5.0)
        command.add_argument("--no-record", action="store_true")

    txlist = sub.add_parser("transactions", help="list persisted transactions")
    txlist.add_argument("--json", action="store_true")

    history = sub.add_parser("history", help="list saved visibility decisions")
    history.add_argument("--limit", type=int)

    metrics = sub.add_parser("metrics", help="show latency, cache, and session metadata")
    metrics.add_argument("--budget-ms", type=float, default=5.0)

    save = sub.add_parser("save-snapshot", help="persist a named immutable session snapshot")
    save.add_argument("--name", required=True)
    save.add_argument("--force", action="store_true")

    restore = sub.add_parser("restore-snapshot", help="validate and restore a snapshot as a whole replacement")
    source = restore.add_mutually_exclusive_group(required=True)
    source.add_argument("--name")
    source.add_argument("--file")
    restore.add_argument("--replay", action="store_true")
    restore.add_argument("--budget-ms", type=float, default=5.0)

    replay = sub.add_parser("replay-history", help="authoritatively replay every persisted decision")
    replay.add_argument("--budget-ms", type=float, default=5.0)

    inject = sub.add_parser("inject-snapshot", help="modify an input snapshot copy with one typed anomaly")
    inject.add_argument("--source", required=True)
    inject.add_argument("--output", required=True)
    inject.add_argument("--kind", choices=("chain", "cache", "txn"), required=True)

    anomaly = sub.add_parser("anomaly-demo", help="run all three injected-input checks against a snapshot")
    anomaly.add_argument("--source", required=True)
    anomaly.add_argument("--output-dir", default="artifacts/injected")
    anomaly.add_argument("--budget-ms", type=float, default=5.0)

    background = sub.add_parser("background-check", help="run concurrent background commits with interleaved verification")
    background.add_argument("--concurrency", type=int, default=3)
    background.add_argument("--commits", type=int, default=8)
    background.add_argument("--interval-ms", type=int, default=10)
    background.add_argument("--budget-ms", type=float, default=5.0)

    demo = sub.add_parser("demo", help="run the full deterministic acceptance workflow and write a report")
    demo.add_argument("--script", default=str(SAMPLE_PATH))
    demo.add_argument("--output", help="write the JSON verification report to this path")
    demo.add_argument("--budget-ms", type=float, default=5.0)

    gui = sub.add_parser("gui", help="launch the Tkinter desktop workbench")
    gui.add_argument("--budget-ms", type=float, default=5.0)
    return parser


def _workbench(args: argparse.Namespace, *, reset: bool = False) -> Workbench:
    return Workbench(
        args.session_dir,
        budget_ms=float(getattr(args, "budget_ms", 5.0)),
        reset=reset,
    )


def _scope(args: argparse.Namespace) -> int | None:
    return None if args.latest else int(args.txid)


def _demo(args: argparse.Namespace) -> dict[str, Any]:
    session_dir = Path(args.session_dir).resolve()
    workbench = Workbench(session_dir, budget_ms=float(args.budget_ms), reset=True)
    script = ScriptRunner.load(args.script)
    sample = ScriptRunner(workbench).run(script)
    if not sample["all_canonical_equal"]:
        raise BusinessError("E_VISIBLE", "sample contains a fast/authoritative mismatch", {})

    repeated_a = workbench.query("inventory", "sku-1", txid=4, record=True)
    repeated_b = workbench.query("inventory", "sku-1", txid=4, record=True)
    repeated_equal = repeated_a["canonical"] == repeated_b["canonical"]
    if not repeated_equal:
        raise BusinessError(
            "E_VISIBLE",
            "same snapshot/key query was not byte-identical on repeat",
            {"txid": 4, "key": "sku-1"},
        )

    snapshot = workbench.save_snapshot("baseline", overwrite=True)
    post = workbench.begin_transaction("post-snapshot-writer")
    workbench.write(
        post["txid"],
        "inventory",
        "sku-post-snapshot",
        {"item": "PostSnapshot", "qty": 99},
    )
    workbench.commit(post["txid"])
    version_before_restore = workbench.state_version
    restored = workbench.restore_snapshot_path(snapshot["path"], replay=True)
    if not restored["version_mismatch"] or not restored["replay"]["matched"] == restored["replay"]["checked"]:
        raise BusinessError("E_REPLAY", "snapshot replacement or replay did not complete", restored)
    restored_metadata = workbench.metadata()

    anomalies = run_anomaly_demo(workbench, snapshot["path"], session_dir / "injected")
    if not anomalies["all_passed"]:
        raise BusinessError("E_INPUT", "one or more anomaly injections did not produce the required code", anomalies)

    concurrency = _background_check(
        workbench,
        concurrency=3,
        commits=8,
        interval_ms=10,
        verify_each=True,
    )
    if not concurrency["all_canonical_equal"]:
        raise BusinessError("E_VISIBLE", "concurrent query verification failed", concurrency)

    recovery_dir = session_dir / "forced-recovery-case"
    crashed = Workbench(recovery_dir, budget_ms=float(args.budget_ms), reset=True)
    crashed.create_table("recovery", [{"name": "key", "type": "TEXT"}])
    orphan = crashed.begin_transaction("orphan-before-force-stop")
    crashed.write(orphan["txid"], "recovery", "orphan", {"state": "uncommitted"})
    orphan_txid = orphan["txid"]
    # Deliberately do not commit or close; a new process sees only persistence.
    recovered = Workbench(recovery_dir, budget_ms=float(args.budget_ms))
    recovered_tx = recovered.transaction(orphan_txid)
    recovered_query = recovered.verify_latest("recovery", "orphan", record=True)
    recovery = {
        "orphan_txid": orphan_txid,
        "state_after_restart": recovered_tx["state"],
        "latest_visible": recovered_query["authoritative"]["visible"],
        "canonical_equal": recovered_query["canonical_equal"],
        "exit_report_generated": False,
    }
    if recovery["state_after_restart"] != "rolled_back" or recovery["latest_visible"]:
        raise BusinessError("E_TXN", "forced-recovery rollback did not take effect", recovery)

    report = {
        "ok": True,
        "sample": {
            "script": sample["script"],
            "steps": sample["steps"],
            "reads": sample["reads"],
            "all_canonical_equal": sample["all_canonical_equal"],
        },
        "repeat_query": {
            "txid": 4,
            "key": "sku-1",
            "byte_identical": repeated_equal,
            "canonical_sha256": repeated_a["canonical_sha256"],
        },
        "snapshot": {
            "path": snapshot["path"],
            "session_version": snapshot["session_version"],
            "version_before_restore": version_before_restore,
            "version_mismatch": restored["version_mismatch"],
            "strategy": restored["strategy"],
            "replay": restored["replay"],
            "state_version_after": restored["state_version_after"],
            "next_txid_after": restored_metadata["next_txid"],
        },
        "anomalies": anomalies,
        "concurrency": concurrency,
        "forced_recovery": recovery,
        "metrics": workbench.metrics_summary(),
        "logs": str(workbench.logger.path),
        "db": str(workbench.db_path),
    }
    if args.output:
        atomic_write_json(args.output, report)
        workbench.logger.event("verification_report_written", path=str(Path(args.output).resolve()))
    return report


def _background_check(
    workbench: Workbench,
    *,
    concurrency: int,
    commits: int,
    interval_ms: int,
    verify_each: bool,
) -> dict[str, Any]:
    commits_events: list[dict[str, Any]] = []

    def callback(event: dict[str, Any]) -> None:
        if event.get("kind") == "background_commit":
            commits_events.append(
                {
                    "sequence": event["sequence"],
                    "txid": event["txid"],
                    "state_version": event["state_version"],
                }
            )

    writer = BackgroundCommitter(
        workbench,
        concurrency=concurrency,
        interval_s=max(0.01, interval_ms / 1000.0),
        max_commits=commits,
        callback=callback,
    )
    writer.start()
    queries = []
    deadline = time.monotonic() + 30.0
    while time.monotonic() < deadline:
        stats = writer.stats()
        if stats["completed"] >= commits:
            break
        response = workbench.verify_latest("background_events", "counter", record=True)
        queries.append(
            {
                "txid": response["txid"],
                "view_version": response["view_version"],
                "canonical_equal": response["canonical_equal"],
                "visible": response["authoritative"]["visible"],
                "value": response["authoritative"]["value"],
                "elapsed_us": response["timing"]["elapsed_us"],
            }
        )
        time.sleep(0.005)
    final_stats = writer.stop()
    final = workbench.verify_latest("background_events", "counter", record=True)
    queries.append(
        {
            "txid": final["txid"],
            "view_version": final["view_version"],
            "canonical_equal": final["canonical_equal"],
            "visible": final["authoritative"]["visible"],
            "value": final["authoritative"]["value"],
            "elapsed_us": final["timing"]["elapsed_us"],
            "final": True,
        }
    )
    return {
        "writer": final_stats,
        "commits": sorted(commits_events, key=lambda row: row["sequence"]),
        "queries": queries,
        "query_count": len(queries),
        "all_canonical_equal": all(row["canonical_equal"] for row in queries),
        "final_visible": final["authoritative"]["visible"],
        "final_value": final["authoritative"]["value"],
    }


def main(argv: Sequence[str] | None = None) -> int:
    parser = _parser()
    args = parser.parse_args(argv)
    session_dir = Path(args.session_dir).resolve()
    try:
        if args.command == "reset":
            if session_dir.exists():
                shutil.rmtree(session_dir)
            _emit({"ok": True, "reset": str(session_dir)})
            return 0
        if args.command == "init":
            workbench = Workbench(session_dir, reset=False)
            _emit({"ok": True, **workbench.metadata()})
            return 0
        if args.command == "demo":
            report = _demo(args)
            _emit(report)
            return 0
        if args.command == "run-script":
            workbench = Workbench(
                session_dir, budget_ms=float(args.budget_ms), reset=bool(args.reset)
            )
            result = ScriptRunner(workbench).run(ScriptRunner.load(args.script))
            if args.snapshot:
                result["snapshot"] = workbench.save_snapshot(args.snapshot)
            result["ok"] = True
            _emit(result)
            return 0
        if args.command in {"query", "verify"}:
            workbench = _workbench(args)
            txid = _scope(args)
            if args.command == "query":
                result = workbench.query(args.table, args.key, txid=txid, record=not args.no_record)
            else:
                result = workbench.verify(args.table, args.key, txid=txid, record=not args.no_record)
            result["ok"] = True
            _emit(result)
            return 0
        if args.command == "transactions":
            workbench = _workbench(args)
            rows = workbench.transactions()
            _emit({"ok": True, "transactions": rows, "count": len(rows)})
            return 0
        if args.command == "history":
            workbench = _workbench(args)
            rows = workbench.history(limit=args.limit)
            _emit({"ok": True, "history": rows, "count": len(rows)})
            return 0
        if args.command == "metrics":
            workbench = _workbench(args)
            _emit({"ok": True, **workbench.metadata()})
            return 0
        if args.command == "save-snapshot":
            workbench = _workbench(args)
            _emit({"ok": True, **workbench.save_snapshot(args.name, overwrite=args.force)})
            return 0
        if args.command == "restore-snapshot":
            workbench = _workbench(args)
            if args.name:
                result = workbench.restore_snapshot(args.name, replay=args.replay)
            else:
                result = workbench.restore_snapshot_path(args.file, replay=args.replay)
            _emit({"ok": True, **result})
            return 0
        if args.command == "replay-history":
            workbench = _workbench(args)
            _emit({"ok": True, **workbench.replay_history()})
            return 0
        if args.command == "inject-snapshot":
            result = inject_snapshot(args.source, args.output, args.kind)
            _emit({"ok": True, **result})
            return 0
        if args.command == "anomaly-demo":
            workbench = _workbench(args)
            result = run_anomaly_demo(workbench, args.source, args.output_dir)
            _emit({"ok": result["all_passed"], **result})
            return 0 if result["all_passed"] else 2
        if args.command == "background-check":
            workbench = _workbench(args)
            result = _background_check(
                workbench,
                concurrency=args.concurrency,
                commits=args.commits,
                interval_ms=args.interval_ms,
                verify_each=True,
            )
            _emit({"ok": result["all_canonical_equal"], **result})
            return 0 if result["all_canonical_equal"] else 2
        if args.command == "gui":
            from .gui import run_gui

            run_gui(session_dir=session_dir, budget_ms=float(args.budget_ms))
            return 0
        parser.error("unknown command")
        return 2
    except BusinessError as exc:
        logger = StructuredLogger(session_dir / "logs" / "events.jsonl")
        logger.event("business_error", level="error", code=exc.code, message=exc.message,
                     details=exc.details, command=args.command)
        _emit({"ok": False, "error": exc.as_dict()})
        return 2
    except Exception as exc:  # pragma: no cover - defensive CLI boundary
        logger = StructuredLogger(session_dir / "logs" / "events.jsonl")
        logger.event("internal_error", level="error", error=repr(exc), command=args.command,
                     traceback=traceback.format_exc())
        _emit({"ok": False, "error": {"code": "E_INTERNAL", "message": repr(exc)}})
        return 3


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
