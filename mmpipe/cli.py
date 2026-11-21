"""Command line entry points for the local multimodal pipeline."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .config import BudgetConfig, PipelineConfig
from .consistency import compare_runs, verify_snapshots
from .errors import PipeError
from .fault_tools import corrupt_snapshot
from .manifest import generate_manifest, load_manifest
from .pipeline import Pipeline
from .scheduler import ResourceObservation, arbitrate


def _build_config(args: argparse.Namespace) -> PipelineConfig:
    return PipelineConfig()


def _build_budget(args: argparse.Namespace) -> BudgetConfig:
    return BudgetConfig(
        max_workers=args.max_workers,
        max_batch_pairs=args.max_batch_pairs,
        max_resident_embeddings=args.max_resident,
        max_stage_pairs=args.max_stage_pairs,
        max_parallel_tasks=args.max_tasks,
        overflow_policy=args.overflow_policy,
    )


def cmd_generate(args: argparse.Namespace) -> int:
    config = _build_config(args)
    records = generate_manifest(
        Path(args.work_dir), config, num_pairs=args.num_pairs, generator_seed=args.generator_seed
    )
    print(json.dumps({"generated": len(records), "work_dir": args.work_dir}, indent=2))
    return 0


def cmd_run(args: argparse.Namespace) -> int:
    config = _build_config(args)
    records = load_manifest(Path(args.work_dir) / "manifest.jsonl")
    pipeline = Pipeline(
        Path(args.work_dir),
        config,
        _build_budget(args),
        requested_workers=args.requested_workers,
        requested_batch=args.requested_batch,
        force_rebuild=args.force_rebuild,
    )
    result = pipeline.run(records)
    print(
        json.dumps(
            {"status": "complete", "scores": dict(sorted(result.scores.items()))},
            sort_keys=True,
            indent=2,
        )
    )
    return 0


def cmd_verify(args: argparse.Namespace) -> int:
    config = _build_config(args)
    records = load_manifest(Path(args.work_dir) / "manifest.jsonl")
    outcome = verify_snapshots(
        Path(args.work_dir), records, config, force_rebuild=args.force_rebuild
    )
    print(json.dumps(outcome, indent=2))
    return 0


def cmd_arbitrate(args: argparse.Namespace) -> int:
    budget = _build_budget(args)
    decision = arbitrate(
        ResourceObservation(
            stage=args.stage,
            pair_count=args.pair_count,
            requested_workers=args.requested_workers,
            requested_batch=args.requested_batch,
        ),
        budget,
    )
    print(json.dumps(decision.to_report(), indent=2))
    return 0


def cmd_compare(args: argparse.Namespace) -> int:
    report_a = json.loads(Path(args.report_a).read_text(encoding="utf-8"))
    report_b = json.loads(Path(args.report_b).read_text(encoding="utf-8"))
    print(json.dumps(compare_runs(report_a, report_b), indent=2))
    return 0


def cmd_corrupt(args: argparse.Namespace) -> int:
    path = corrupt_snapshot(Path(args.work_dir), args.stage, mode=args.mode)
    print(json.dumps({"corrupted": str(path), "stage": args.stage, "mode": args.mode}, indent=2))
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="mmpipe", description="Deterministic local multimodal inference pipeline"
    )
    sub = parser.add_subparsers(dest="command", required=True)

    def add_budget_flags(p: argparse.ArgumentParser) -> None:
        p.add_argument("--max-workers", type=int, default=2)
        p.add_argument("--max-batch-pairs", type=int, default=4)
        p.add_argument("--max-resident", type=int, default=64)
        p.add_argument("--max-stage-pairs", type=int, default=256)
        p.add_argument("--max-tasks", type=int, default=8)
        p.add_argument("--overflow-policy", choices=("degrade", "reject"), default="degrade")
        p.add_argument("--requested-workers", type=int, default=2)
        p.add_argument("--requested-batch", type=int, default=4)
        p.add_argument("--force-rebuild", action="store_true")

    gen = sub.add_parser("generate", help="generate deterministic samples")
    gen.add_argument("work_dir")
    gen.add_argument("--num-pairs", type=int, default=12)
    gen.add_argument("--generator-seed", type=int, default=777)
    gen.set_defaults(func=cmd_generate)

    run = sub.add_parser("run", help="run (or resume) the pipeline")
    run.add_argument("work_dir")
    add_budget_flags(run)
    run.set_defaults(func=cmd_run)

    verify = sub.add_parser("verify", help="independently verify snapshots and recovery")
    verify.add_argument("work_dir")
    verify.add_argument("--force-rebuild", action="store_true")
    verify.set_defaults(func=cmd_verify)

    arb = sub.add_parser("arbitrate", help="inspect a budget arbitration decision")
    arb.add_argument("--stage", default="image")
    arb.add_argument("--pair-count", type=int, default=12)
    add_budget_flags(arb)
    arb.set_defaults(func=cmd_arbitrate)

    cmp_p = sub.add_parser("compare", help="compare two run reports byte-for-byte")
    cmp_p.add_argument("report_a")
    cmp_p.add_argument("report_b")
    cmp_p.set_defaults(func=cmd_compare)

    cor = sub.add_parser("corrupt", help="inject a local snapshot fault for recovery tests")
    cor.add_argument("work_dir")
    cor.add_argument("stage", choices=("image", "text", "fusion", "score"))
    cor.add_argument("--mode", choices=("truncate", "tamper", "manifest"), default="tamper")
    cor.set_defaults(func=cmd_corrupt)
    return parser


def main(argv=None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return args.func(args)
    except PipeError as err:
        print(json.dumps(err.to_dict(), sort_keys=True, indent=2), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())

