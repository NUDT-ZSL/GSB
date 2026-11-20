"""Command-line entry point that writes a reproducible JSON report."""

from __future__ import annotations

import argparse
from pathlib import Path
from typing import Sequence

from .config import NumericsConfig, ROUNDING_MODES, SUBNORMAL_POLICIES
from .manifest import (
    load_manifest_with_duplicates,
    write_manifest,
)
from .models import JobSpec
from .reporting import build_report
from .samples import SAMPLE_JOB_MAPPINGS
from .util import canonical_json_text


PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_JOBS = PROJECT_ROOT / "samples" / "tasks.jsonl"
DEFAULT_REPORT = PROJECT_ROOT / "artifacts" / "report.json"


def sample_specs() -> list[JobSpec]:
    return [JobSpec.from_mapping(value) for value in SAMPLE_JOB_MAPPINGS]


def ensure_sample_manifest(path: Path) -> list[JobSpec]:
    specs = sample_specs()
    write_manifest(path, specs)
    return specs


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Run deterministic embedding gather/reduction diagnostics and "
            "write a JSON report."
        )
    )
    parser.add_argument(
        "--jobs",
        type=Path,
        default=DEFAULT_JOBS,
        help=f"JSONL task manifest (default: {DEFAULT_JOBS.name})",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=DEFAULT_REPORT,
        help=f"JSON report path (default: {DEFAULT_REPORT.name})",
    )
    parser.add_argument(
        "--regenerate-samples",
        action="store_true",
        help="rewrite the built-in deterministic manifest before running",
    )
    parser.add_argument(
        "--rounding-mode",
        choices=ROUNDING_MODES,
        default="nearest_even",
    )
    parser.add_argument(
        "--dequant-rounding-step",
        type=float,
        default=0.0,
    )
    parser.add_argument(
        "--accum-rounding-step",
        type=float,
        default=0.0,
    )
    parser.add_argument(
        "--subnormal-policy",
        choices=SUBNORMAL_POLICIES,
        default="preserve",
    )
    parser.add_argument(
        "--subnormal-threshold",
        type=float,
        default=2.2250738585072014e-308,
    )
    parser.add_argument(
        "--pad-policy",
        choices=("exclude", "zero_weight"),
        default="exclude",
    )
    parser.add_argument(
        "--tolerance-abs",
        type=float,
        default=1e-12,
    )
    parser.add_argument(
        "--tolerance-rel",
        type=float,
        default=1e-9,
    )
    parser.add_argument(
        "--compact",
        action="store_true",
        help="write canonical compact JSON instead of indented JSON",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    jobs_path = Path(args.jobs)
    output_path = Path(args.output)
    if args.regenerate_samples or not jobs_path.exists():
        specs = ensure_sample_manifest(jobs_path)
        duplicate_errors: list[dict[str, object]] = []
    else:
        specs, duplicate_errors = load_manifest_with_duplicates(jobs_path)
    config = NumericsConfig(
        rounding_mode=args.rounding_mode,
        dequant_rounding_step=args.dequant_rounding_step,
        accum_rounding_enabled=args.accum_rounding_step > 0.0,
        accum_rounding_step=args.accum_rounding_step,
        subnormal_policy=args.subnormal_policy,
        subnormal_threshold=args.subnormal_threshold,
        pad_policy=args.pad_policy,
        tolerance_abs=args.tolerance_abs,
        tolerance_rel=args.tolerance_rel,
    )
    report = build_report(specs, config, duplicate_errors=duplicate_errors)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    text = canonical_json_text(report, pretty=not args.compact)
    if not args.compact:
        text = "\n".join(
            line.rstrip() for line in text.splitlines()
        )
    output_path.write_text(text + "\n", encoding="utf-8", newline="\n")
    summary = report["summary"]
    print(
        "wrote "
        f"{output_path} | jobs={summary['succeeded']}/{summary['total_jobs']} "
        f"degraded={summary['degraded']} "
        f"injections={summary['injections_raised']}/"
        f"{summary['injections_total']} "
        f"codes={len(summary['business_codes'])}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
