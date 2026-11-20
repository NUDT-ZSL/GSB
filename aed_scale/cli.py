"""Command-line entry points for generation, execution and the complete demo."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Sequence

from .adaptive import ResourceConstraints
from .codes import BusinessCode
from .config import DetectionConfig, FeatureConfig
from .errors import AedError
from .io import write_json
from .pipeline import run_dataset
from .synthesis import generate_dataset


def _summary(report: dict[str, Any]) -> dict[str, Any]:
    frame = report["metrics"]["frame"]
    event = report["metrics"]["event"]
    return {
        "selected_path": report["selected_path"],
        "auto_selected_path": report["auto_selected_path"],
        "degraded": report["degraded"],
        "business_code": report["business_code"],
        "scale_observation": report["scale_observation"],
        "decision_reasons": report["tradeoff_decision"]["decision_reasons"],
        "frame_accuracy": round(float(frame["frame_accuracy"]), 9),
        "event_frame_recall": round(float(frame["event_frame_recall"]), 9),
        "event_recall": round(float(event["event_recall"]), 9),
        "event_precision": round(float(event["event_precision"]), 9),
        "event_f1": round(float(event["event_f1"]), 9),
        "truth_event_count": event["truth_count"],
        "predicted_event_count": event["prediction_count"],
        "matched_event_count": event["matched_count"],
        "consistency": report["consistency"]["status"],
    }


def _constraints_from_args(args: argparse.Namespace) -> ResourceConstraints:
    return ResourceConstraints(
        max_resident_mb=args.max_resident_mb,
        max_candidate_comparisons=args.max_candidate_comparisons,
        max_fine_frames=args.max_fine_frames,
        min_recall_gain=args.min_recall_gain,
    )


def _error_payload(error: AedError, report_path: str | Path | None = None) -> dict[str, Any]:
    payload = {
        "business_code": error.code,
        "degraded": True,
        "errors": [error.to_dict()],
    }
    if report_path is not None:
        write_json(report_path, payload)
    return payload


def _generate_command(args: argparse.Namespace) -> dict[str, Any]:
    return {
        "business_code": BusinessCode.OK.value,
        "degraded": False,
        "dataset": generate_dataset(
            args.out,
            seed=args.seed,
            num_dialogs=args.dialogs,
            duration_ms=args.duration_ms,
            speaker_count=args.speakers,
        ),
    }


def _run_command(args: argparse.Namespace) -> dict[str, Any]:
    report = run_dataset(
        args.dataset,
        config=FeatureConfig(feature_mode="full"),
        detection_config=DetectionConfig(),
        constraints=_constraints_from_args(args),
        recall_gain_estimate=args.recall_gain_estimate,
        path_override=args.path_override,
    )
    write_json(args.report, report)
    return {
        "business_code": report["business_code"],
        "degraded": report["degraded"],
        "report_path": str(Path(args.report)),
        "summary": _summary(report),
    }


def _demo_command(args: argparse.Namespace) -> dict[str, Any]:
    root = Path(args.out)
    root.mkdir(parents=True, exist_ok=True)
    small_dir = root / "datasets" / "small"
    large_dir = root / "datasets" / "large"
    small_dataset = generate_dataset(
        small_dir,
        seed=args.seed,
        num_dialogs=2,
        duration_ms=2_200,
        speaker_count=3,
    )
    large_dataset = generate_dataset(
        large_dir,
        seed=args.seed,
        num_dialogs=8,
        duration_ms=3_000,
        speaker_count=4,
    )
    constraints = ResourceConstraints(
        max_resident_mb=0.94,
        max_candidate_comparisons=5_000,
        max_fine_frames=1_000,
        min_recall_gain=0.02,
    )
    small_auto = run_dataset(
        small_dir,
        constraints=constraints,
        recall_gain_estimate=0.05,
    )
    small_approximate = run_dataset(
        small_dir,
        constraints=constraints,
        recall_gain_estimate=0.05,
        path_override="approximate",
    )
    large_auto = run_dataset(
        large_dir,
        constraints=constraints,
        recall_gain_estimate=0.05,
    )
    small_summary = _summary(small_auto)
    approximate_summary = _summary(small_approximate)
    large_summary = _summary(large_auto)
    report = {
        "business_code": BusinessCode.OK.value,
        "degraded": bool(small_auto["degraded"] or large_auto["degraded"]),
        "seed": args.seed,
        "constraints": constraints.to_dict(),
        "summary": {
            "small": small_summary,
            "large": large_summary,
            "small_fine_vs_approximate": {
                "fine": small_summary,
                "approximate": approximate_summary,
                "frame_accuracy_delta": round(
                    small_summary["frame_accuracy"] - approximate_summary["frame_accuracy"],
                    9,
                ),
                "event_recall_delta": round(
                    small_summary["event_recall"] - approximate_summary["event_recall"],
                    9,
                ),
            },
        },
        "datasets": {
            "small": small_dataset,
            "large": large_dataset,
        },
        "runs": {
            "small_auto": small_auto,
            "small_approximate_override": small_approximate,
            "large_auto": large_auto,
        },
        "consistency": {
            "small": small_auto["consistency"],
            "large": large_auto["consistency"],
        },
        "errors": [],
    }
    write_json(root / "report.json", report)
    write_json(root / "summary.json", report["summary"])
    return {
        "business_code": BusinessCode.OK.value,
        "degraded": report["degraded"],
        "report_path": str(root / "report.json"),
        "summary": report["summary"],
    }


def _add_budget_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--max-resident-mb", type=float, default=8.0)
    parser.add_argument("--max-candidate-comparisons", type=int, default=2_000_000)
    parser.add_argument("--max-fine-frames", type=int, default=50_000)
    parser.add_argument("--min-recall-gain", type=float, default=0.02)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m aed_scale",
        description="Deterministic scale-adaptive audio event detection",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    generate = subparsers.add_parser("generate", help="generate a local synthetic dataset")
    generate.add_argument("--out", required=True)
    generate.add_argument("--seed", type=int, default=20261121)
    generate.add_argument("--dialogs", type=int, default=2)
    generate.add_argument("--duration-ms", type=int, default=2_200)
    generate.add_argument("--speakers", type=int, default=3)

    run = subparsers.add_parser("run", help="run the component on a dataset")
    run.add_argument("--dataset", required=True)
    run.add_argument("--report", required=True)
    run.add_argument("--recall-gain-estimate", type=float, default=0.05)
    run.add_argument("--path-override", choices=("fine", "approximate"))
    _add_budget_arguments(run)

    demo = subparsers.add_parser("demo", help="run the complete small/large-scale demo")
    demo.add_argument("--out", default="artifacts/demo")
    demo.add_argument("--seed", type=int, default=20261121)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        if args.command == "generate":
            result = _generate_command(args)
        elif args.command == "run":
            result = _run_command(args)
        elif args.command == "demo":
            result = _demo_command(args)
        else:
            parser.error(f"unknown command {args.command}")
            return 2
    except AedError as exc:
        report_path = getattr(args, "report", None)
        payload = _error_payload(exc, report_path=report_path)
        print(json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2), file=sys.stderr)
        return 2
    print(json.dumps(result, ensure_ascii=False, sort_keys=True, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
