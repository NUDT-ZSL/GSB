"""Command-line entry point."""

from __future__ import annotations

import argparse
import multiprocessing
import sys
from pathlib import Path
from typing import Any

from .canonical import canonical_json_bytes
from .dataset import generate_dataset_file
from .engine import attach_reproducibility_report, run_experiment
from .errors import EngineError
from .validation import load_dataset, load_json_document


def _write_payload(payload: dict[str, Any], output: str | None) -> None:
    encoded = canonical_json_bytes(payload) + b"\n"
    if output:
        destination = Path(output)
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(encoded)
    else:
        sys.stdout.buffer.write(encoded)
        sys.stdout.buffer.flush()


def _load_core(dataset_path: str, baseline_path: str, conflict_path: str) -> dict[str, Any]:
    dataset = load_dataset(dataset_path)
    baseline_config = load_json_document(baseline_path, stage="config")
    conflict_config = load_json_document(conflict_path, stage="config")
    return run_experiment(
        baseline_config=baseline_config,
        conflict_config=conflict_config,
        dataset=dataset,
    )


def _command_generate(args: argparse.Namespace) -> dict[str, Any]:
    return {
        "ok": True,
        "command": "generate",
        "dataset": generate_dataset_file(
            args.output,
            seed=args.seed,
            n_total=args.samples,
            n_clusters=args.clusters,
            n_features=args.features,
            anomaly_ratio=args.anomaly_ratio,
        ),
    }


def _command_run(args: argparse.Namespace) -> dict[str, Any]:
    core = _load_core(args.dataset, args.baseline_config, args.conflict_config)
    if args.check_rerun:
        repeated = _load_core(args.dataset, args.baseline_config, args.conflict_config)
        return attach_reproducibility_report(core, repeat_core=repeated, repeats=2)
    return core


def _command_validate(args: argparse.Namespace) -> dict[str, Any]:
    dataset = load_dataset(args.dataset)
    baseline_config = load_json_document(args.baseline_config, stage="config")
    conflict_config = load_json_document(args.conflict_config, stage="config")
    from .config import normalize_config

    normalized_baseline = normalize_config(baseline_config, n_rows=dataset.n_rows)
    normalized_conflict = normalize_config(conflict_config, n_rows=dataset.n_rows)
    return {
        "ok": True,
        "command": "validate",
        "dataset": {
            "n_samples": dataset.n_rows,
            "n_features": dataset.n_features,
            "n_injected_anomalies": sum(dataset.injected),
            "source_sha256": dataset.source_sha256,
        },
        "configs": {
            "baseline_candidates": len(normalized_baseline.candidates),
            "conflict_candidates": len(normalized_conflict.candidates),
        },
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="anomaly-engine",
        description="Deterministic budget-aware anomaly detection experiment runner",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    generate = subparsers.add_parser("generate", help="generate a deterministic JSONL dataset")
    generate.add_argument("--output", default="data/demo.jsonl")
    generate.add_argument("--seed", type=int, default=20261121)
    generate.add_argument("--samples", type=int, default=600)
    generate.add_argument("--clusters", type=int, default=6)
    generate.add_argument("--features", type=int, default=5)
    generate.add_argument("--anomaly-ratio", type=float, default=0.08)
    generate.set_defaults(handler=_command_generate)

    run = subparsers.add_parser("run", help="run baseline and budget-conflict scenarios")
    run.add_argument("--dataset", default="data/demo.jsonl")
    run.add_argument("--baseline-config", default="configs/demo.json")
    run.add_argument("--conflict-config", default="configs/demo_conflict.json")
    run.add_argument("--output", default="artifacts/experiment.json")
    run.add_argument(
        "--check-rerun",
        action="store_true",
        help="execute the whole experiment twice and verify canonical output hashes",
    )
    run.set_defaults(handler=_command_run)

    validate = subparsers.add_parser("validate", help="validate samples and both configs")
    validate.add_argument("--dataset", default="data/demo.jsonl")
    validate.add_argument("--baseline-config", default="configs/demo.json")
    validate.add_argument("--conflict-config", default="configs/demo_conflict.json")
    validate.add_argument("--output")
    validate.set_defaults(handler=_command_validate)

    return parser


def main(argv: list[str] | None = None) -> int:
    multiprocessing.freeze_support()
    parser = build_parser()
    args = parser.parse_args(argv)
    output = getattr(args, "output", None)
    try:
        payload = args.handler(args)
    except EngineError as exc:
        payload = exc.to_dict()
        _write_payload(payload, output)
        return 2
    except Exception as exc:
        payload = EngineError(
            "E_INTERNAL",
            "cli",
            f"{type(exc).__name__}: {exc}",
        ).to_dict()
        _write_payload(payload, output)
        return 3

    if hasattr(args, "output") and args.command != "generate":
        _write_payload(payload, output)
    else:
        _write_payload(payload, None)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
