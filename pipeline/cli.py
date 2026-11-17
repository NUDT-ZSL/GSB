"""Command line entry points for generation and pipeline execution."""

from __future__ import annotations

import argparse
import json
import sys

from .atomicio import atomic_write_json, atomic_write_text
from .config import load_fusion_config
from .engine import GracefulExit, run_pipeline
from .hashing import content_hash
from .injection import load_injection_config
from .samples import generate_sample_tree
from .textrel import load_relation_table


def cmd_generate(args) -> int:
    config = load_fusion_config(args.config)
    names = generate_sample_tree(
        output_dir=args.input_dir,
        sample_count=args.samples,
        width=config.image_width,
        height=config.image_height,
        seed=config.rng_seed,
        grid_rows=config.grid_rows,
        grid_cols=config.grid_cols,
    )
    # Sample name + round manifest used to compare against the serial baseline.
    manifest = []
    for name in names:
        for round_index in range(config.max_rounds):
            manifest.append({"sample": name, "round": round_index,
                             "sort_key": [name, round_index]})
    atomic_write_json(args.manifest, {
        "sample_count": len(names),
        "max_rounds": config.max_rounds,
        "ordering": "sample_name_then_round",
        "entries": manifest,
    })
    print(f"generated {len(names)} samples; manifest -> {args.manifest}")
    return 0


def cmd_run(args) -> int:
    try:
        _path, _version, signaled = run_pipeline(
            input_dir=args.input_dir,
            config_path=args.config,
            relation_path=args.relations,
            injection_path=args.injections,
            work_dir=args.work_dir,
            report_path=args.report,
        )
    except GracefulExit:
        # Snapshot and failure list are already durable; exit code 0 and no
        # final report is promised for a handleable interruption.
        print("graceful exit: snapshot and failure list persisted",
              file=sys.stderr)
        return 0
    if signaled:
        print("graceful exit: snapshot and failure list persisted",
              file=sys.stderr)
        return 0
    print(f"report -> {args.report}")
    return 0


def cmd_hashes(args) -> int:
    config = load_fusion_config(args.config)
    relations = load_relation_table(args.relations)
    injections = load_injection_config(args.injections)
    payload = {
        "config_hash": config.hash(),
        "relation_hash": content_hash(
            [item.to_dict() for item in relations]),
        "injection_hash": content_hash(
            [item.to_dict() for item in injections]) if injections
        else content_hash([]),
    }
    atomic_write_text(args.output, json.dumps(payload, indent=2, sort_keys=True))
    print(json.dumps(payload, indent=2, sort_keys=True))
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="evidence-pipeline",
        description="Deterministic image-text evidence joint decision pipeline")
    sub = parser.add_subparsers(dest="command", required=True)

    gen = sub.add_parser("generate", help="generate deterministic samples")
    gen.add_argument("--input-dir", required=True)
    gen.add_argument("--config", required=True)
    gen.add_argument("--samples", type=int, default=8)
    gen.add_argument("--manifest", required=True)
    gen.set_defaults(func=cmd_generate)

    run = sub.add_parser("run", help="run the evidence pipeline")
    run.add_argument("--input-dir", required=True)
    run.add_argument("--config", required=True)
    run.add_argument("--relations", required=True)
    run.add_argument("--injections")
    run.add_argument("--work-dir", required=True)
    run.add_argument("--report", required=True)
    run.set_defaults(func=cmd_run)

    hashes = sub.add_parser("hashes", help="print config/relation hashes")
    hashes.add_argument("--config", required=True)
    hashes.add_argument("--relations", required=True)
    hashes.add_argument("--injections")
    hashes.add_argument("--output", required=True)
    hashes.set_defaults(func=cmd_hashes)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
