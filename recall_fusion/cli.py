"""Command line entry point for sample generation and pipeline runs."""

from __future__ import annotations

import argparse
import json
import sys

from .config import FusionConfig
from .pipeline import FusionPipeline
from .sample_data import generate_dataset, load_dataset, write_default_config


def cmd_generate(args: argparse.Namespace) -> int:
    paths = generate_dataset(
        args.data_dir, n_docs=args.docs, n_queries=args.queries, seed=args.seed
    )
    config_path = write_default_config(args.config)
    print(json.dumps({k: str(v) for k, v in {**paths, "config": config_path}.items()},
                     indent=2, ensure_ascii=False))
    return 0


def cmd_run(args: argparse.Namespace) -> int:
    docs, queries = load_dataset(args.data_dir)
    config = FusionConfig.load(args.config)
    pipeline = FusionPipeline(docs, config, in_process=args.in_process)
    report = pipeline.run(queries)
    paths = pipeline.save_artifacts(args.out_dir, report)
    summary = {
        "completed": report.stats["completed"],
        "rejected": report.stats["rejected"],
        "worker_errors": len(report.worker_errors),
        "evictions": len(report.eviction_events),
        "arbitrations": len(report.arbitrations),
        "artifacts": {k: str(v) for k, v in paths.items()},
    }
    print(json.dumps(summary, indent=2, ensure_ascii=False))
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="recall_fusion",
        description="Deterministic multi-channel recall fusion pipeline",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    gen = sub.add_parser("generate", help="generate deterministic samples and config")
    gen.add_argument("--data-dir", default="data")
    gen.add_argument("--config", default="config/fusion_config.json")
    gen.add_argument("--docs", type=int, default=120)
    gen.add_argument("--queries", type=int, default=25)
    gen.add_argument("--seed", type=int, default=20261124)
    gen.set_defaults(func=cmd_generate)

    run = sub.add_parser("run", help="run the fusion pipeline")
    run.add_argument("--data-dir", default="data")
    run.add_argument("--config", default="config/fusion_config.json")
    run.add_argument("--out-dir", default="artifacts/run")
    run.add_argument(
        "--in-process",
        action="store_true",
        help="disable process pool (single process execution)",
    )
    run.set_defaults(func=cmd_run)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    # Required guard for Windows "spawn" process-pool startup.
    sys.exit(main())
