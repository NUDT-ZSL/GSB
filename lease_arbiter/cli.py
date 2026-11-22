from __future__ import annotations

import argparse
import json
import os
import sys
import traceback
from typing import Any

from .engine import ArbiterEngine, RenewRequest
from .errors import BizError, Code, ValidationFailure
from .fragments import analyze_waiting_job
from .ledger import Ledger
from .validation import load_bundle
from .verify import Verification


DEFAULT_DB = "var/ledger.db"
DEFAULT_POINTER = "var/ledger.pointer.json"
DEFAULT_REPORTS = "reports"
DEFAULT_DATA = "data"


def _emit(value: Any) -> None:
    print(json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2))


def _ledger(args: argparse.Namespace) -> Ledger:
    return Ledger(args.db, args.pointer, args.reports)


def _parse_renewals(values: list[str]) -> list[RenewRequest]:
    result: list[RenewRequest] = []
    for value in values:
        parts = value.split(":")
        if len(parts) != 3 or not parts[2].isdigit() or int(parts[2]) < 1:
            raise BizError(
                Code.PROTOCOL,
                "renewal must be LEASE_ID:JOB_ID:GENERATION",
                details={"value": value},
            )
        result.append(
            RenewRequest(
                lease_id=parts[0],
                job_id=parts[1],
                generation=int(parts[2]),
            )
        )
    return result


def _report_summary(report: dict[str, Any]) -> dict[str, Any]:
    return {
        "ok": True,
        "round": report.get("round"),
        "commit_key": report.get("commit_key"),
        "operation": report.get("operation"),
        "ledger_version": report.get("ledger_version"),
        "report_digest": report.get("report_digest"),
        "clock": report.get("clock"),
        "renewals": report.get("renewals", []),
        "expirations": report.get("expirations", []),
        "grants": report.get("grants", []),
        "denials": report.get("denials", []),
        "fragment_analyses": report.get("fragment_analyses", []),
        "reclaimed_fragments": report.get("reclaimed_fragments", []),
        "reclaim_summary": report.get("reclaim_summary", []),
        "anomalies": report.get("anomalies", []),
        "selection_chain": report.get("selection_chain", []),
        "grantable_after_preemption": report.get("grantable_after_preemption"),
        "persisted": report.get("persisted", True),
    }


def command_load(args: argparse.Namespace) -> dict[str, Any]:
    bundle = load_bundle(args.cards, args.jobs, args.config, args.anomalies)
    ledger = _ledger(args)
    with ledger.lock():
        result = ledger.load_bundle(bundle, force=args.force)
    return {
        "ok": True,
        "command": "load",
        "source_hashes": bundle.source_hashes,
        **result,
    }


def command_lease(args: argparse.Namespace) -> dict[str, Any]:
    engine = ArbiterEngine(_ledger(args))
    report = engine.run_lease_round(
        args.round, _parse_renewals(args.renew)
    )
    return _report_summary(report)


def command_renew(args: argparse.Namespace) -> dict[str, Any]:
    engine = ArbiterEngine(_ledger(args))
    request = RenewRequest(
        lease_id=args.lease_id,
        job_id=args.job_id,
        generation=args.generation,
    )
    report = engine.renew_round(args.round, request)
    return _report_summary(report)


def command_preempt(args: argparse.Namespace) -> dict[str, Any]:
    engine = ArbiterEngine(_ledger(args))
    apply = not args.dry_run
    report = engine.preempt_round(args.round, args.job_id, apply=apply)
    summary = _report_summary(report)
    summary["initial_constraint"] = report.get("initial_constraint")
    summary["final_constraint"] = report.get("final_constraint")
    summary["evaluated"] = report.get("evaluated", [])
    summary["refund_records"] = report.get("refund_records", [])
    summary["grantable_without_preemption"] = report.get(
        "grantable_without_preemption", False
    )
    summary["dry_run"] = args.dry_run
    return summary


def command_resume(args: argparse.Namespace) -> dict[str, Any]:
    ledger = _ledger(args)
    with ledger.lock():
        connection = ledger.connect()
        try:
            recovery = ledger.recover(connection)
            latest = ledger.latest_round(connection)
            round_no = args.round
            if round_no is None:
                round_no = int(latest["round_no"]) if latest is not None else 1
            already_committed = (
                ledger.committed_round(connection, f"lease:{round_no:04d}")
                is not None
            )
        finally:
            connection.close()
    engine = ArbiterEngine(ledger)
    report = engine.run_lease_round(round_no, _parse_renewals(args.renew))
    return {
        "ok": True,
        "command": "resume",
        "recovery": recovery,
        "already_committed": already_committed,
        "replayed": not already_committed,
        "report": _report_summary(report),
    }


def command_status(args: argparse.Namespace) -> dict[str, Any]:
    ledger = _ledger(args)
    with ledger.lock():
        connection = ledger.connect()
        try:
            meta = {
                row["key"]: row["value"]
                for row in connection.execute("SELECT * FROM meta")
            }
            pointer = ledger.pointer_snapshot()
            latest = ledger.latest_round(connection)
            active = [
                dict(row)
                for row in connection.execute(
                    "SELECT lease_id, job_id, card_id, compute_start, compute_length, "
                    "memory_start, memory_length, generation, expires_at_ms, priority, "
                    "quota_group FROM leases WHERE status = 'active' ORDER BY lease_id"
                )
            ]
            waiting = [
                dict(row)
                for row in connection.execute(
                    "SELECT job_id, priority, quota_group, compute_shares, "
                    "memory_shares, last_round, failure_json FROM jobs "
                    "WHERE state = 'waiting' ORDER BY priority_rank DESC, job_id"
                )
            ]
            quota = [
                dict(row)
                for row in connection.execute(
                    "SELECT * FROM quota_groups ORDER BY quota_group"
                )
            ]
            anomalies = [
                {
                    **json.loads(row["details_json"]),
                    "commit_key": row["commit_key"],
                }
                for row in connection.execute(
                    "SELECT * FROM anomaly_fires ORDER BY round_no, card_id, anomaly_id"
                )
            ]
            refunds = [
                dict(row)
                for row in connection.execute(
                    "SELECT * FROM refunds WHERE status = 'pending' ORDER BY refund_id"
                )
            ]
            jobs = {
                row["state"]: row["count"]
                for row in connection.execute(
                    "SELECT state, COUNT(*) AS count FROM jobs GROUP BY state"
                )
            }
            version = int(meta["ledger_version"])
            pointer_consistent = bool(
                pointer
                and pointer.get("version") == version
                and (
                    latest is None
                    or pointer.get("commit_key") == latest["commit_key"]
                )
            )
            return {
                "ok": True,
                "command": "status",
                "ledger_version": version,
                "clock_watermark_ms": int(meta["clock_watermark_ms"]),
                "latest_commit": latest["commit_key"] if latest else None,
                "pointer_consistent": pointer_consistent,
                "pointer": pointer,
                "job_states": jobs,
                "active_leases": active,
                "waiting_jobs": waiting,
                "quota_groups": quota,
                "pending_refunds": refunds,
                "anomalies": anomalies,
            }
        finally:
            connection.close()


def command_verify(args: argparse.Namespace) -> dict[str, Any]:
    result = Verification(_ledger(args)).run()
    result["command"] = "verify"
    return result


def command_recombine(args: argparse.Namespace) -> dict[str, Any]:
    ledger = _ledger(args)
    engine = ArbiterEngine(ledger)
    with ledger.lock():
        connection = ledger.connect()
        try:
            config = engine._config(connection)
            cards = engine._cards(connection)
            job_row = connection.execute(
                "SELECT * FROM jobs WHERE job_id = ?", (args.job_id,)
            ).fetchone()
            if job_row is None:
                raise BizError(
                    Code.PROTOCOL,
                    "job does not exist",
                    details={"job_id": args.job_id},
                )
            job = engine._job(job_row)
            free_map = engine._free_map(
                cards, config, engine._active_leases(connection)
            )
            analysis = analyze_waiting_job(
                job,
                cards,
                config,
                free_map,
                round_no=args.round,
                injections=engine._injections(connection, args.round),
            )
            return {
                "ok": True,
                "command": "recombine",
                "job_id": job.job_id,
                "analysis": analysis,
                "anomalies": analysis["anomalies"],
            }
        finally:
            connection.close()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="split-lease",
        description="Deterministic local split-share accelerator lease arbiter",
    )
    parser.add_argument("--db", default=DEFAULT_DB)
    parser.add_argument("--pointer", default=DEFAULT_POINTER)
    parser.add_argument("--reports", default=DEFAULT_REPORTS)
    sub = parser.add_subparsers(dest="command", required=True)

    load = sub.add_parser("load", help="validate and load deterministic JSON samples")
    load.add_argument("--cards", default=f"{DEFAULT_DATA}/accelerator_cards.json")
    load.add_argument("--jobs", default=f"{DEFAULT_DATA}/jobs.json")
    load.add_argument("--config", default=f"{DEFAULT_DATA}/lease_config.json")
    load.add_argument("--anomalies", default=f"{DEFAULT_DATA}/anomaly_injections.json")
    load.add_argument("--force", action="store_true")
    load.set_defaults(func=command_load)

    lease = sub.add_parser("lease", help="run one logical-clock lease round")
    lease.add_argument("--round", type=int, required=True)
    lease.add_argument(
        "--renew",
        action="append",
        default=[],
        metavar="LEASE_ID:JOB_ID:GENERATION",
    )
    lease.set_defaults(func=command_lease)

    renew = sub.add_parser("renew", help="renew one lease as part of a round")
    renew.add_argument("--round", type=int, required=True)
    renew.add_argument("--lease-id", required=True)
    renew.add_argument("--job-id", required=True)
    renew.add_argument("--generation", type=int, required=True)
    renew.set_defaults(func=command_renew)

    preempt = sub.add_parser("preempt", help="plan or apply preemption for a job")
    preempt.add_argument("--round", type=int, required=True)
    preempt.add_argument("--job-id", required=True)
    preempt.add_argument(
        "--dry-run",
        action="store_true",
        help="only print the recovery chain; do not persist preemption or grant",
    )
    preempt.add_argument(
        "--apply",
        action="store_true",
        help="accepted for compatibility; apply is the default",
    )
    preempt.set_defaults(func=command_preempt)

    resume = sub.add_parser("resume", help="repair pointer and replay an uncommitted round")
    resume.add_argument("--round", type=int)
    resume.add_argument(
        "--renew",
        action="append",
        default=[],
        metavar="LEASE_ID:JOB_ID:GENERATION",
    )
    resume.set_defaults(func=command_resume)

    status = sub.add_parser("status", help="show ledger, quota and waiting-job status")
    status.set_defaults(func=command_status)

    verify = sub.add_parser("verify", help="verify overlap, conservation and refunds")
    verify.set_defaults(func=command_verify)

    recombine = sub.add_parser(
        "recombine", help="analyze whether a waiting job can use recovered fragments"
    )
    recombine.add_argument("--round", type=int, required=True)
    recombine.add_argument("--job-id", required=True)
    recombine.set_defaults(func=command_recombine)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        result = args.func(args)
        _emit(result)
        if args.command == "verify" and not result.get("ok", False):
            return 2
        return 0
    except ValidationFailure as exc:
        _emit({"ok": False, "command": args.command, "error": exc.to_dict()})
        return 2
    except BizError as exc:
        _emit(
            {
                "ok": False,
                "command": args.command,
                "error": exc.to_dict(),
            }
        )
        return 2
    except Exception as exc:
        if os.environ.get("LEASE_ARBITER_DEBUG") == "1":
            traceback.print_exc()
        _emit(
            {
                "ok": False,
                "command": args.command,
                "error": {
                    "code": "ARB-INT-001",
                    "message": "unexpected internal error",
                    "details": {"type": type(exc).__name__, "reason": str(exc)},
                    "round": None,
                    "card_id": None,
                },
            }
        )
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
