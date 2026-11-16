"""Command line entry point with integer exit-code semantics."""

from __future__ import annotations

import argparse
import json
import os
import sys
from typing import Any

from .config import load_config
from .runner import BenchmarkRunner
from .samplegen import SCALE_PROFILES, generate_sample_workspace
from .snapshot import SnapshotMismatch
from .util import read_json

EXIT_OK = 0
EXIT_RUNTIME = 1
EXIT_SNAPSHOT = 2
EXIT_USAGE = 64


def _load_workspace(sample_dir: str, results_dir: str, config_path: str) -> tuple[
    BenchmarkRunner, dict[str, Any]
]:
    config = load_config(config_path)
    manifest_path = os.path.join(sample_dir, "generation_manifest.json")
    profiles_path = os.path.join(sample_dir, "scale_profiles.json")
    manifest = read_json(manifest_path) if os.path.exists(manifest_path) else {"scales": []}
    scales = read_json(profiles_path) if os.path.exists(profiles_path) else SCALE_PROFILES
    scale_by_id = {row["scale_id"]: row for row in manifest.get("scales", [])}
    for scale in scales:
        if scale["scale_id"] in scale_by_id:
            scale.update({
                "log_path": scale_by_id[scale["scale_id"]]["log_path"],
                "dictionary_path": scale_by_id[scale["scale_id"]]["dictionary_path"],
                "input_fingerprint": scale_by_id[scale["scale_id"]]["input_fingerprint"],
            })
        else:
            scale["log_path"] = os.path.join(sample_dir, scale["scale_id"], "interactions.jsonl")
            scale["dictionary_path"] = os.path.join(sample_dir, scale["scale_id"], "items.jsonl")
    runner = BenchmarkRunner(sample_dir, results_dir, config, scales, manifest)
    return runner, config


def cmd_generate(args: argparse.Namespace) -> int:
    config = load_config(args.config)
    manifest = generate_sample_workspace(args.sample_dir, config)
    print(f"generated samples under {args.sample_dir}; scales={len(manifest['scales'])}")
    return EXIT_OK


def cmd_run(args: argparse.Namespace) -> int:
    runner, _config = _load_workspace(args.sample_dir, args.results_dir, args.config)
    scale_filter = args.scales.split(",") if args.scales else None
    report = runner.run(scale_filter=scale_filter)
    incremental = report["incremental"]
    print(
        f"run finished: scales={len(report['scale_metrics'])} "
        f"reused={incremental['reused_count']} recomputed={incremental['recomputed_count']} "
        f"report={os.path.join(args.results_dir, 'reports', 'comparison_report.json')}"
    )
    return EXIT_OK


def cmd_verify(args: argparse.Namespace) -> int:
    left = read_json(args.left)
    right = read_json(args.right)
    ignore = {"report_fingerprint", "incremental"}
    comparable_left = {k: v for k, v in left.items() if k not in ignore}
    comparable_right = {k: v for k, v in right.items() if k not in ignore}
    if comparable_left == comparable_right:
        print("VERIFY OK: reports are field-identical (excluding cache ledger/fingerprint)")
        return EXIT_OK
    print("VERIFY FAILED: reports differ")
    _summarize_diff(comparable_left, comparable_right)
    return EXIT_RUNTIME


def _summarize_diff(left: Any, right: Any, path: str = "$") -> None:
    if type(left) is not type(right):
        print(f"  type diff at {path}: {type(left).__name__} vs {type(right).__name__}")
        return
    if isinstance(left, dict):
        for key in sorted(set(left) | set(right)):
            if key not in left or key not in right:
                print(f"  key diff at {path}.{key}")
            else:
                _summarize_diff(left[key], right[key], f"{path}.{key}")
    elif isinstance(left, list):
        if len(left) != len(right):
            print(f"  length diff at {path}: {len(left)} vs {len(right)}")
        for index, (a, b) in enumerate(zip(left, right)):
            _summarize_diff(a, b, f"{path}[{index}]")
    elif left != right:
        print(f"  value diff at {path}: {left!r} vs {right!r}")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="recall_bench", description="multi-channel recall workbench")
    sub = parser.add_subparsers(dest="command", required=True)

    gen = sub.add_parser("generate", help="generate deterministic samples")
    gen.add_argument("--sample-dir", required=True)
    gen.add_argument("--config", required=True)
    gen.set_defaults(func=cmd_generate)

    run = sub.add_parser("run", help="run the recall/fusion/evaluation pipeline")
    run.add_argument("--sample-dir", required=True)
    run.add_argument("--results-dir", required=True)
    run.add_argument("--config", required=True)
    run.add_argument("--scales", default="", help="comma separated scale ids, default: all")
    run.set_defaults(func=cmd_run)

    verify = sub.add_parser("verify", help="compare two reports for field-level equality")
    verify.add_argument("--left", required=True)
    verify.add_argument("--right", required=True)
    verify.set_defaults(func=cmd_verify)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return int(args.func(args))
    except SnapshotMismatch as exc:
        print(f"{exc.code}: {exc}", file=sys.stderr)
        return EXIT_SNAPSHOT
    except (FileNotFoundError, ValueError, KeyError, json.JSONDecodeError) as exc:
        print(f"ERROR: {type(exc).__name__}: {exc}", file=sys.stderr)
        return EXIT_RUNTIME


if __name__ == "__main__":
    sys.exit(main())
