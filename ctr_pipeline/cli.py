from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from .canonical import checksum
from .errors import PipelineError
from .generator import generate_dataset
from .ingestion import expand_dataset, load_feature_dictionary, load_source_samples
from .models import BudgetConfig, TrainingConfig
from .pipeline import load_result, run_pipeline
from .snapshots import SnapshotStore
from .validation import validate_result


def _print_json(value: Any) -> None:
    print(json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True))


def _print_error(error: PipelineError) -> None:
    print(
        json.dumps(error.to_dict(), ensure_ascii=False, indent=2, sort_keys=True),
        file=sys.stderr,
    )


def _generate(args: argparse.Namespace) -> int:
    manifest = generate_dataset(
        args.out,
        seed=args.seed,
        sample_count=args.samples,
        dictionary_version=args.dict_version,
    )
    _print_json(manifest)
    return 0


def _run(args: argparse.Namespace) -> int:
    config = TrainingConfig(
        chunk_size=args.chunk_size,
        batch_sample_size=args.batch_size,
        workers=args.workers,
        max_rounds=args.rounds,
        learning_rate=args.learning_rate,
        tolerance=args.tolerance,
        l2=args.l2,
        fault_chunk_ids=tuple(args.fault_chunk),
    )
    budget = BudgetConfig(
        total_mac_budget=args.mac_budget,
        memory_budget_bytes=args.memory_budget,
        allow_degraded=not args.strict_budget,
    )
    result = run_pipeline(
        args.exposures,
        args.features,
        feature_dimension=args.feature_dim,
        dictionary_version=args.dict_version,
        max_cross_slots=args.max_cross_slots,
        training_config=config,
        budget_config=budget,
        output_dir=args.out,
        snapshot_dir=args.snapshot_dir,
        snapshot_version=args.snapshot_version,
        submission_lock_path=args.submission_lock,
    )
    comparison = result.get("comparison")
    comparison_summary: dict[str, Any] | None = None
    if comparison is not None:
        comparison_summary = {
            "chunked_converged_round": comparison["chunked_converged_round"],
            "converged_round_equal": comparison["converged_round_equal"],
            "full_converged_round": comparison["full_converged_round"],
            "max_abs_parameter_deviation": comparison[
                "max_abs_parameter_deviation"
            ],
            "mean_abs_parameter_deviation": comparison[
                "mean_abs_parameter_deviation"
            ],
            "parameter_deviation_spectrum_size": len(
                comparison["final_parameter_abs_deviation_spectrum"]
            ),
            "round_count": len(comparison["round_deltas"]),
        }
    decision = result["budget"]["decision"]
    _print_json(
        {
            "admitted_samples": decision["admitted_sample_count"],
            "budget_error_code": result["summary"]["budget_error_code"],
            "comparison": comparison_summary,
            "degrade_categories": result["summary"]["degrade_categories"],
            "degraded": result["summary"]["degraded"],
            "determinism_fingerprint": result["determinism_fingerprint"],
            "effective_chunk_size": decision["effective_chunk_size"],
            "effective_workers": decision["effective_workers"],
            "estimated_macs": decision["estimated_macs"],
            "estimated_memory_bytes": decision["estimated_memory_bytes"],
            "metrics": result["summary"]["metrics"],
            "rejected_samples": decision["rejected_sample_count"],
            "sample_count": result["summary"]["sample_count"],
            "status": result["status"],
            "validation_status": result["validation"]["status"],
        }
    )
    if result["status"] == "partial":
        return 3
    if result["status"] == "budget_rejected":
        return 4
    return 0


def _validate(args: argparse.Namespace) -> int:
    result = load_result(args.result)
    dictionary = load_feature_dictionary(
        args.features,
        version=args.dict_version,
    )
    source_samples = load_source_samples(
        args.exposures,
        dictionary,
        max_cross_slots=args.max_cross_slots,
    )
    expanded = expand_dataset(
        source_samples,
        dictionary,
        feature_dimension=args.feature_dim,
    )
    report = validate_result(result, expanded_samples=expanded.samples)
    if args.full:
        _print_json(report)
    else:
        reduction = report.get("recomputed", {}).get("reduction") or {}
        final_parameters = reduction.get("final_parameters")
        _print_json(
            {
                "checks": report["checks"],
                "metrics": report.get("recomputed", {}).get("metrics"),
                "reduction": {
                    "round_count": len(reduction.get("rounds", [])),
                    "final_parameter_count": (
                        len(final_parameters) if final_parameters is not None else 0
                    ),
                    "final_parameters_checksum": (
                        checksum(final_parameters)
                        if final_parameters is not None
                        else None
                    ),
                },
                "status": report["status"],
            }
        )
    return 0


def _snapshot_check(args: argparse.Namespace) -> int:
    store = SnapshotStore(args.directory)
    loaded = store.load_all()
    _print_json(
        {
            name: {
                "checksum": snapshot["checksum"],
                "version": snapshot["version"],
            }
            for name, snapshot in sorted(loaded.items())
        }
    )
    return 0


def _acceptance(args: argparse.Namespace) -> int:
    from .acceptance import run_acceptance

    report = run_acceptance(Path(args.out))
    _print_json(report)
    return 0 if report["all_passed"] else 1


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="ctr-pipeline",
        description="Deterministic local CTR training pipeline",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    generate = subparsers.add_parser("generate", help="generate deterministic samples")
    generate.add_argument("--out", default="data")
    generate.add_argument("--seed", type=int, default=20261121)
    generate.add_argument("--samples", type=int, default=120)
    generate.add_argument("--dict-version", default="dict-v1")
    generate.set_defaults(handler=_generate)

    run = subparsers.add_parser("run", help="run the complete training pipeline")
    run.add_argument("--exposures", default="data/exposures.jsonl")
    run.add_argument("--features", default="data/features.jsonl")
    run.add_argument("--feature-dim", type=int, default=256)
    run.add_argument("--dict-version", default="dict-v1")
    run.add_argument("--max-cross-slots", type=int, default=8)
    run.add_argument("--chunk-size", type=int, default=8)
    run.add_argument("--batch-size", type=int, default=24)
    run.add_argument("--workers", type=int, default=2)
    run.add_argument("--rounds", type=int, default=16)
    run.add_argument("--learning-rate", type=float, default=0.35)
    run.add_argument("--tolerance", type=float, default=1e-9)
    run.add_argument("--l2", type=float, default=0.0)
    run.add_argument("--mac-budget", type=int, default=50_000_000)
    run.add_argument("--memory-budget", type=int, default=32 * 1024 * 1024)
    run.add_argument("--strict-budget", action="store_true")
    run.add_argument("--fault-chunk", type=int, action="append", default=[])
    run.add_argument("--out", default="results/latest")
    run.add_argument("--snapshot-dir", default=None)
    run.add_argument("--snapshot-version", type=int, default=1)
    run.add_argument("--submission-lock", default=None)
    run.set_defaults(handler=_run)

    validate = subparsers.add_parser("validate", help="independently validate a result")
    validate.add_argument("--result", required=True)
    validate.add_argument("--exposures", default="data/exposures.jsonl")
    validate.add_argument("--features", default="data/features.jsonl")
    validate.add_argument("--feature-dim", type=int, default=256)
    validate.add_argument("--dict-version", default="dict-v1")
    validate.add_argument("--max-cross-slots", type=int, default=8)
    validate.add_argument("--full", action="store_true")
    validate.set_defaults(handler=_validate)

    snapshot = subparsers.add_parser(
        "snapshot-check",
        help="load and verify snapshot checksums",
    )
    snapshot.add_argument("--directory", default="snapshots")
    snapshot.set_defaults(handler=_snapshot_check)

    acceptance = subparsers.add_parser(
        "acceptance",
        help="run the reproducible local acceptance suite",
    )
    acceptance.add_argument("--out", default="results/acceptance")
    acceptance.set_defaults(handler=_acceptance)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return int(args.handler(args))
    except PipelineError as exc:
        _print_error(exc)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
