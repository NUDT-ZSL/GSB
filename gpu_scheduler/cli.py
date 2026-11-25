from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Dict, Optional, Sequence

from .engine import SchedulerEngine
from .errors import SchedulerError
from .models import load_bundle
from .storage import SchedulerStore


DEFAULT_DB = Path("results/scheduler.db")
DEFAULT_SAMPLES = Path("data/samples")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="gpu-scheduler",
        description="Deterministic local GPU scheduling and quota engine",
    )
    parser.add_argument("--db", default=str(DEFAULT_DB), help="SQLite ledger path")
    parser.add_argument("--samples", default=str(DEFAULT_SAMPLES), help="sample directory")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("load", help="validate samples and initialize SQLite ledger")

    grant = sub.add_parser("grant", help="grant one job or a deterministic batch")
    grant.add_argument("--round", required=True)
    grant.add_argument("--now", type=int)
    grant.add_argument("--job", action="append", default=[], dest="jobs")
    grant.add_argument("--all", action="store_true", help="process all jobs in stable ID order")
    grant.add_argument("--max-grants", type=int)
    grant.add_argument("--card", help="optional card pin; default is global deterministic selection")
    grant.add_argument("--request-key")

    renew = sub.add_parser("renew", help="renew an active lease with generation fencing")
    renew.add_argument("--round", required=True)
    renew.add_argument("--now", type=int, required=True)
    renew.add_argument("--job", required=True)
    renew.add_argument("--generation", required=True, type=int)
    renew.add_argument("--request-key")

    recover = sub.add_parser("recover", help="expire/recycle leases or shrink a quota group")
    recover.add_argument("--round", required=True)
    recover.add_argument("--now", type=int, required=True)
    recover.add_argument(
        "--reason",
        choices=["normal_expiry", "quota_shrink"],
        default="normal_expiry",
    )
    recover.add_argument("--quota-group")
    recover.add_argument("--compute-limit", type=int)
    recover.add_argument("--reorg-job", help="optionally run post-recycle reorganization for this job")
    recover.add_argument("--reorg-card")
    recover.add_argument("--request-key")

    reorganize = sub.add_parser("reorganize", help="fragment-aware reorganization planning")
    reorganize.add_argument("--round", required=True)
    reorganize.add_argument("--now", type=int, required=True)
    reorganize.add_argument("--job", required=True)
    reorganize.add_argument("--card")
    reorganize.add_argument("--max-migrations", type=int, default=4)
    reorganize.add_argument("--request-key")

    preempt = sub.add_parser("preempt", help="dry-run or execute preemption chain")
    preempt.add_argument("--job", required=True)
    preempt.add_argument("--now", type=int, required=True)
    preempt.add_argument("--round")
    preempt.add_argument("--execute", action="store_true")
    preempt.add_argument("--plan-digest", help="require dry-run digest before execution")
    preempt.add_argument("--max-victims", type=int)
    preempt.add_argument("--request-key")

    sub.add_parser("resume", help="recover planned/applied operations after forced termination")

    status = sub.add_parser("status", help="stable sorted JSON status")
    status.add_argument("--output")

    verify = sub.add_parser("verify", help="replay ledger chain and verify all invariants")
    verify.add_argument("--output")
    verify.add_argument(
        "--self-test",
        action="store_true",
        help="also run deterministic fresh-DB algorithm and fault checks",
    )

    return parser


def print_json(payload: Dict[str, Any], output: Optional[str] = None) -> None:
    text = json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True, default=str)
    if output:
        path = Path(output)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text + "\n", encoding="utf-8")
    print(text)


def make_engine(args: argparse.Namespace) -> SchedulerEngine:
    bundle = load_bundle(Path(args.samples))
    return SchedulerEngine(SchedulerStore(args.db), bundle)


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        if args.command == "load":
            # Validate before constructing/opening the database so invalid input
            # cannot leave even an empty ledger file behind.
            bundle = load_bundle(Path(args.samples))
            engine = SchedulerEngine(SchedulerStore(args.db), bundle)
            result = engine.initialize_bundle()
            print_json(result)
            return 0

        engine = make_engine(args)

        if args.command == "grant":
            if not args.jobs and not args.all:
                raise SchedulerError(
                    "grant requires --job ID or --all",
                    error_code="E_GRANT_TARGET_REQUIRED",
                    exit_code=22,
                )
            if args.all or len(args.jobs) > 1:
                request = {
                    "action": "grant_batch",
                    "stage": "grant",
                    "round_id": args.round,
                    "now_ms": args.now,
                    "job_ids": args.jobs if args.jobs else None,
                    "max_grants": args.max_grants,
                    "card_id": args.card,
                    "request_key": args.request_key,
                }
            else:
                request = {
                    "action": "grant",
                    "stage": "grant",
                    "round_id": args.round,
                    "now_ms": args.now,
                    "job_id": args.jobs[0],
                    "card_id": args.card,
                    "request_key": args.request_key,
                }
            result = engine.submit({k: v for k, v in request.items() if v is not None})
            print_json(result)
            return 0

        if args.command == "renew":
            result = engine.submit(
                {
                    "action": "renew",
                    "stage": "renew",
                    "round_id": args.round,
                    "now_ms": args.now,
                    "job_id": args.job,
                    "generation": args.generation,
                    "request_key": args.request_key,
                }
            )
            print_json(result)
            return 0

        if args.command == "recover":
            if args.reason == "quota_shrink" and (not args.quota_group or args.compute_limit is None):
                raise SchedulerError(
                    "quota_shrink requires --quota-group and --compute-limit",
                    error_code="E_QUOTA_SHRINK_ARGUMENTS",
                    exit_code=22,
                )
            result = engine.submit(
                {
                    "action": "recover",
                    "stage": "recover",
                    "round_id": args.round,
                    "now_ms": args.now,
                    "reason": args.reason,
                    "quota_group": args.quota_group,
                    "compute_limit": args.compute_limit,
                    "reorg_job_id": args.reorg_job,
                    "reorg_card_id": args.reorg_card,
                    "request_key": args.request_key,
                }
            )
            print_json(result)
            return 0

        if args.command == "reorganize":
            result = engine.submit(
                {
                    "action": "reorganize",
                    "stage": "reorganize",
                    "round_id": args.round,
                    "now_ms": args.now,
                    "job_id": args.job,
                    "card_id": args.card,
                    "max_migrations": args.max_migrations,
                    "request_key": args.request_key,
                }
            )
            print_json(result)
            return 0

        if args.command == "preempt":
            if not args.execute:
                result = engine.plan_preemption_now(args.job, args.now, max_victims=args.max_victims)
                print_json(result)
                return 0
            if not args.round:
                raise SchedulerError(
                    "preempt --execute requires --round",
                    error_code="E_PREEMPT_ROUND_REQUIRED",
                    exit_code=22,
                )
            result = engine.submit(
                {
                    "action": "preempt_execute",
                    "stage": "preempt",
                    "round_id": args.round,
                    "now_ms": args.now,
                    "job_id": args.job,
                    "expected_plan_digest": args.plan_digest,
                    "max_victims": args.max_victims,
                    "request_key": args.request_key,
                }
            )
            print_json(result)
            return 0

        if args.command == "resume":
            result = engine.resume()
            print_json(result)
            return 0 if result["ok"] else 50

        if args.command == "status":
            result = engine.status()
            print_json(result, args.output)
            return 0

        if args.command == "verify":
            from .verify import run_verification

            result = run_verification(
                db_path=args.db,
                sample_dir=args.samples,
                self_test=args.self_test,
            )
            print_json(result, args.output)
            return 0 if result["ok"] else 50

        parser.error(f"unknown command: {args.command}")
        return 2

    except SchedulerError as exc:
        payload = exc.to_dict()
        payload["exit_code"] = exc.exit_code
        print(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True), file=sys.stderr)
        return exc.exit_code


if __name__ == "__main__":
    raise SystemExit(main())
