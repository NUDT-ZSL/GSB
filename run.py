#!/usr/bin/env python3
"""Command-line entry point for the diagnostic engine."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from emotion_diag.canonical import atomic_write_json
from emotion_diag.config import EngineConfig
from emotion_diag.errors import DiagnosticError
from emotion_diag.incremental import run_generation_update, run_truth_correction
from emotion_diag.pipeline import run_pipeline
from emotion_diag.samplegen import generate_sample_bundle
from emotion_diag.snapshots import ForcedTermination


def _ensure_samples(samples: Path, config_path: str | None, seed: int | None) -> None:
    if (samples / "manifest.json").exists():
        return
    config = EngineConfig.load(config_path)
    if seed is not None:
        config.values["seed"] = seed
    generate_sample_bundle(samples, config)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--mode",
        choices=["generate", "full", "incremental", "all"],
        default="full",
    )
    parser.add_argument("--samples", default="samples")
    parser.add_argument("--output", default="artifacts")
    parser.add_argument("--config", default=None)
    parser.add_argument("--seed", type=int, default=None)
    parser.add_argument("--workers", type=int, default=None)
    parser.add_argument("--no-process-pool", action="store_true")
    parser.add_argument("--crash-stage", default=None)
    parser.add_argument("--crash-after-frame", type=int, default=300)
    parser.add_argument("--hard-crash", action="store_true")
    args = parser.parse_args(argv)

    samples = Path(args.samples)
    output = Path(args.output)

    try:
        if args.mode == "generate":
            config = EngineConfig.load(args.config)
            if args.seed is not None:
                config.values["seed"] = args.seed
            manifest = generate_sample_bundle(samples, config)
            print(json.dumps({"ok": True, "manifest": manifest}, indent=2))
            return 0

        _ensure_samples(samples, args.config, args.seed)
        crash = None
        if args.crash_stage:
            crash = {
                "stage": args.crash_stage,
                "after_frame": args.crash_after_frame,
                "hard": bool(args.hard_crash),
            }

        report = run_pipeline(
            samples,
            output,
            config_path=args.config,
            workers=args.workers,
            use_process_pool=False if args.no_process_pool else None,
            crash=crash,
        )

        incremental_reports = None
        if args.mode in {"incremental", "all"}:
            truth_report = run_truth_correction(samples, output)
            generation_report = run_generation_update(samples, output)
            incremental_reports = {
                "truth_correction": truth_report,
                "generation_update": generation_report,
            }
            atomic_write_json(output / "incremental_all.json", incremental_reports)

        summary = {
            "ok": True,
            "output": str(output),
            "frame_count": report["frame_count"],
            "anomaly_count": report["anomalies"]["anomaly_count"],
            "cross_version_equivalent": report["migration"]["cross_version_equivalent"],
            "report": str(output / "diagnostic_report.json"),
        }
        if incremental_reports:
            summary["incremental"] = {
                "truth_recomputed_frames": incremental_reports["truth_correction"][
                    "recomputed_state_frames"
                ],
                "generation_recomputed_entries": incremental_reports[
                    "generation_update"
                ]["recomputed_ledger_entries"],
            }
        print(json.dumps(summary, ensure_ascii=False, indent=2))
        return 0
    except ForcedTermination as exc:
        print(
            json.dumps(
                {
                    "ok": False,
                    "forced_termination": True,
                    "stage": exc.stage,
                    "after_frame": exc.after_frame,
                }
            ),
            file=sys.stderr,
        )
        return 137
    except DiagnosticError as exc:
        print(json.dumps(exc.to_dict(), ensure_ascii=False, indent=2), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
