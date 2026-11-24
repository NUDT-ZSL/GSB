"""Command line entry points for the OSL prototype."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from .checkpoint import CheckpointManager
from .engine import BudgetConfig, ProcessJob, process_files
from .generator import DEFAULT_SEED, generate_all, write_root_injection_plan
from .ledger import Ledger
from .storage import atomic_write_json
from .verifier import verify_injection_tree


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="osl-forensics")
    sub = parser.add_subparsers(dest="command", required=True)

    generate = sub.add_parser("generate", help="generate deterministic samples and markers")
    generate.add_argument("--out", default="samples")
    generate.add_argument("--seed", type=int, default=DEFAULT_SEED)
    generate.add_argument("--plan", default="anomaly_injections.json")

    verify = sub.add_parser("verify-injections", help="compare parser output with sync markers")
    verify.add_argument("--root", default="samples/injections")
    verify.add_argument("--report")

    process = sub.add_parser("process", help="process injection samples into SQLite ledger")
    process.add_argument("--root", default="samples/injections")
    process.add_argument("--db", default=".osl-work/ledger.sqlite")
    process.add_argument("--workdir", default=".osl-work")
    process.add_argument("--report", default=".osl-work/batch-report.json")
    process.add_argument("--concurrency", type=int, default=2)
    process.add_argument("--timeout-seconds", type=float, default=5.0)
    process.add_argument("--queue", type=int, default=128)
    process.add_argument("--max-expanded-bytes", type=int, default=None)
    process.add_argument("--no-checkpoint", action="store_true")

    recover = sub.add_parser("recover", help="recover ledger from last complete checkpoint")
    recover.add_argument("--workdir", default=".osl-work")
    recover.add_argument("--db", default=".osl-work/ledger.sqlite")

    revise = sub.add_parser("revise-fixture", help="render the deterministic revision closure")
    revise.add_argument("--fixture", default="samples/revision_fixture.json")
    revise.add_argument("--out", default=".osl-work/revision-closure.json")

    args = parser.parse_args(argv)
    if args.command == "generate":
        plan_path = write_root_injection_plan(args.plan)
        manifest = generate_all(args.out, seed=args.seed)
        print(json.dumps({"ok": True, "plan": str(plan_path), "samples": manifest["sample_count"]}))
        return 0
    if args.command == "verify-injections":
        report = verify_injection_tree(args.root)
        if args.report:
            atomic_write_json(args.report, report)
        print(json.dumps({k: report[k] for k in ("ok", "total", "passed", "failed")}, sort_keys=True))
        return 0 if report["ok"] else 1
    if args.command == "process":
        return _process(args)
    if args.command == "recover":
        manager = CheckpointManager(args.workdir)
        recovered = manager.recover(args.db)
        replay = recovered.ledger.verify_replay()
        result = {
            "ok": replay["ok"],
            "sequence": recovered.recovered_from_sequence,
            "restored_snapshot": recovered.restored_snapshot,
            "replay": replay,
        }
        print(json.dumps(result, sort_keys=True))
        return 0 if replay["ok"] else 1
    if args.command == "revise-fixture":
        fixture = json.loads(Path(args.fixture).read_text(encoding="utf-8"))
        atomic_write_json(args.out, fixture["expected_closure"])
        print(json.dumps(
            {
                "recompute": fixture["expected_closure"]["recompute_segments"],
                "out": args.out,
            },
            ensure_ascii=False,
            sort_keys=True,
        ))
        return 0
    return 2


def _process(args: argparse.Namespace) -> int:
    root = Path(args.root)
    jobs: list[ProcessJob] = []
    for marker_path in sorted(root.rglob("sync_marker.json")):
        marker = json.loads(marker_path.read_text(encoding="utf-8"))
        sample_path = marker_path.parent / marker["sample_file"]
        jobs.append(ProcessJob(file_id=marker["case_id"], path=sample_path))

    budget = BudgetConfig(
        concurrency=args.concurrency,
        timeout_seconds=args.timeout_seconds,
        max_queue=args.queue,
        max_expanded_bytes=args.max_expanded_bytes,
    )
    Path(args.db).parent.mkdir(parents=True, exist_ok=True)
    with Ledger(args.db) as ledger:
        manager = None if args.no_checkpoint else CheckpointManager(args.workdir)
        outcome = process_files(
            jobs,
            budget=budget,
            ledger=ledger,
            checkpoint_manager=manager,
            report_path=args.report,
        )
        replay = ledger.verify_replay()
    summary = {
        "report": args.report,
        "accepted": outcome.report["accepted_jobs"],
        "rejected": outcome.report["rejected_jobs"],
        "counts": outcome.report["counts"],
        "replay_ok": replay["ok"],
    }
    print(json.dumps(summary, ensure_ascii=False, sort_keys=True))
    return 0 if replay["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
