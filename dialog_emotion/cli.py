from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from .cache import CacheStore
from .config import PipelineConfig
from .demo import run_demo
from .errors import DomainError
from .lineage import LineageStore
from .pipeline import DialogueEmotionPipeline
from .synth import append_registry_file, generate_dataset
from .util import atomic_write_json, canonical_json_text


def _print_json(value: Any) -> None:
    print(canonical_json_text(value, indent=2))


def _write_report(path: str | Path | None, value: Any) -> None:
    if path:
        atomic_write_json(path, value)


def _config_from_args(args: argparse.Namespace) -> PipelineConfig:
    return PipelineConfig(
        sample_rate=args.sample_rate,
        frame_ms=args.frame_ms,
        frame_shift_ms=args.frame_shift_ms,
        segment_threshold_ms=args.segment_threshold_ms,
        max_feature_memory_mb=args.max_feature_memory_mb,
        max_cache_entries=args.max_cache_entries,
        max_parallel_nodes=args.max_parallel_nodes,
        requested_parallel_nodes=args.requested_parallel_nodes,
        resource_policy=args.resource_policy,
    )


def _command_generate(args: argparse.Namespace) -> int:
    manifest = generate_dataset(args.out, force=args.force)
    _print_json({"business_code": "OK", "dataset": str(Path(args.out).resolve()), "manifest": manifest})
    return 0


def _command_append_registration(args: argparse.Namespace) -> int:
    row = append_registry_file(
        args.dataset,
        args.out,
        {
            "speaker_id": args.speaker_id,
            "ref_seed": args.ref_seed,
            "ref_duration_ms": args.ref_duration_ms,
            "embedding_version": args.embedding_version,
        },
        args.source,
    )
    _print_json({"business_code": "OK", "output": str(Path(args.out).resolve()), "appended": row})
    return 0


def _command_analyze(args: argparse.Namespace) -> int:
    try:
        config = _config_from_args(args)
        pipeline = DialogueEmotionPipeline(args.cache, args.lineage)
        report = pipeline.analyze(
            args.dataset,
            config,
            registry_path=args.registry,
            corrections_path=args.corrections,
            use_cache=not args.no_cache,
            verify_full_recompute=args.verify_full_recompute,
            write_lineage=not args.no_cache,
            write_state=not args.no_state,
        )
        _write_report(args.out, report)
        _print_json(report)
        return 0
    except DomainError as exc:
        error = exc.to_dict()
        _write_report(args.out, error)
        _print_json(error)
        return 2


def _command_query(args: argparse.Namespace) -> int:
    try:
        store = LineageStore(args.lineage)
        cache = CacheStore(args.cache, read_only=True)
        result = store.replay(args.score_id, cache)
        _print_json(result)
        return 0
    except DomainError as exc:
        _print_json(exc.to_dict())
        return 2


def _command_demo(args: argparse.Namespace) -> int:
    try:
        report = run_demo(args.workspace)
        summary = {
            "business_code": report["business_code"],
            "report": str((Path(args.workspace) / "artifacts" / "demo_report.json").resolve()),
            "runs": {name: run["summary"] for name, run in report["runs"].items()},
            "lineage_replay": report["lineage_replay"]["summary"],
            "error_cases": {
                "passed": report["error_cases"]["passed"],
                "failed": report["error_cases"]["failed"],
            },
        }
        _print_json(summary)
        return 0 if report["business_code"] == "OK" else 2
    except DomainError as exc:
        _print_json(exc.to_dict())
        return 2


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="dialog-emotion")
    subparsers = parser.add_subparsers(dest="command", required=True)

    generate = subparsers.add_parser("generate", help="generate deterministic synthetic dialogue samples")
    generate.add_argument("--out", default="data")
    generate.add_argument("--force", action="store_true")
    generate.set_defaults(handler=_command_generate)

    analyze = subparsers.add_parser("analyze", help="run the dialogue emotion DAG")
    analyze.add_argument("--dataset", default="data")
    analyze.add_argument("--cache", default="artifacts/cache")
    analyze.add_argument("--lineage", default="artifacts/lineage")
    analyze.add_argument("--registry")
    analyze.add_argument("--corrections")
    analyze.add_argument("--out", default="artifacts/report.json")
    analyze.add_argument("--sample-rate", type=int, default=16000)
    analyze.add_argument("--frame-ms", type=float, default=20.0)
    analyze.add_argument("--frame-shift-ms", type=float, default=20.0)
    analyze.add_argument("--segment-threshold-ms", type=float, default=100.0)
    analyze.add_argument("--max-feature-memory-mb", type=float, default=64.0)
    analyze.add_argument("--max-cache-entries", type=int, default=10000)
    analyze.add_argument("--max-parallel-nodes", type=int, default=2)
    analyze.add_argument("--requested-parallel-nodes", type=int, default=1)
    analyze.add_argument("--resource-policy", choices=("error", "degrade"), default="error")
    analyze.add_argument("--no-cache", action="store_true")
    analyze.add_argument("--no-state", action="store_true")
    analyze.add_argument("--verify-full-recompute", action="store_true")
    analyze.set_defaults(handler=_command_analyze)

    query = subparsers.add_parser("query", help="query and replay an immutable lineage score")
    query.add_argument("--lineage", default="artifacts/lineage")
    query.add_argument("--cache", default="artifacts/cache")
    query.add_argument("--score-id", required=True)
    query.set_defaults(handler=_command_query)

    append = subparsers.add_parser("append-registration", help="append a deterministic registration sample")
    append.add_argument("--dataset", default="data")
    append.add_argument("--source", default=None)
    append.add_argument("--out", required=True)
    append.add_argument("--speaker-id", required=True)
    append.add_argument("--ref-seed", type=int, required=True)
    append.add_argument("--ref-duration-ms", type=int, default=1000)
    append.add_argument("--embedding-version", default="embed-v1")
    append.set_defaults(handler=_command_append_registration)

    demo = subparsers.add_parser("demo", help="run the complete local acceptance demo")
    demo.add_argument("--workspace", default=".")
    demo.set_defaults(handler=_command_demo)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return int(args.handler(args))


if __name__ == "__main__":
    sys.exit(main())
