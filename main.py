#!/usr/bin/env python3
"""Command-line entry point for the deterministic local VAD rule engine."""

from __future__ import annotations

import argparse
import json
import shutil
import sqlite3
import sys
from pathlib import Path

import numpy as np

from generate_samples import generate_all
from vad_engine import EngineConfig
from vad_engine.drift import replay_all
from vad_engine.errors import E_STATE_INCONSISTENT, EngineError
from vad_engine.pipeline import Pipeline
from vad_engine.util import load_json_file, require_list, require_mapping
from vad_engine.verification import run_recovery_drill


def _ensure_environment() -> None:
    if sys.version_info < (3, 10):
        raise SystemExit("Python 3.10+ is required")
    _ = np.__version__
    _ = sqlite3.sqlite_version


def _build_config(args: argparse.Namespace) -> EngineConfig:
    config = EngineConfig(
        frame_length_ms=args.frame_length_ms,
        frame_shift_ms=args.frame_shift_ms,
        drift_abs_tolerance=args.drift_abs_tolerance,
        drift_rel_tolerance=args.drift_rel_tolerance,
        max_envelope_memory_bytes=args.max_envelope_memory_mb * 1024 * 1024,
        max_cache_entries=args.max_cache_entries,
        max_parallel_chunks=args.max_parallel_chunks,
        parallel_chunks=args.parallel_chunks,
        strict_resources=args.strict_resources,
    )
    config.validate()
    return config


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-dir", default="data", help="generated sample directory")
    parser.add_argument("--state-dir", default="state", help="persistent snapshot directory")
    parser.add_argument("--report-dir", default="reports", help="report output directory")
    parser.add_argument("--seed", type=int, default=20261126, help="sample root seed")
    parser.add_argument(
        "--regenerate",
        action="store_true",
        help="regenerate deterministic samples before running",
    )
    parser.add_argument(
        "--reset-state",
        action="store_true",
        help="delete persistent state and reports before running",
    )
    parser.add_argument(
        "--skip-recovery",
        action="store_true",
        help="skip the forced-termination and cache replacement drill",
    )
    parser.add_argument("--frame-length-ms", type=int, default=20)
    parser.add_argument("--frame-shift-ms", type=int, default=10)
    parser.add_argument("--drift-abs-tolerance", type=float, default=0.018)
    parser.add_argument("--drift-rel-tolerance", type=float, default=0.22)
    parser.add_argument("--max-envelope-memory-mb", type=int, default=64)
    parser.add_argument("--max-cache-entries", type=int, default=20_000)
    parser.add_argument("--max-parallel-chunks", type=int, default=4)
    parser.add_argument("--parallel-chunks", type=int, default=1)
    parser.add_argument(
        "--strict-resources",
        action="store_true",
        help="turn soft parallel over-budget degradation into E_RESOURCE_LIMIT",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="print the final summary as canonical JSON",
    )
    parser.add_argument(
        "--replay-report",
        help="replay events from an existing drift_report.json without running stages",
    )
    return parser


def _run(args: argparse.Namespace) -> dict:
    _ensure_environment()
    data_dir = Path(args.data_dir)
    state_dir = Path(args.state_dir)
    report_dir = Path(args.report_dir)

    config = _build_config(args)
    if args.replay_report:
        payload = require_mapping(
            load_json_file(args.replay_report, context="drift report"),
            "drift report",
        )
        events = require_list(payload.get("events"), "drift report events")
        replay = replay_all(events, data_dir, config)
        return {
            "business_code": "OK",
            "business_codes": ["OK"],
            "stage_versions": {},
            "stage_reused": {},
            "sessions": len({event.get("session_id") for event in events}),
            "blocks": 0,
            "drift_events": len(events),
            "verification": {"replay": replay},
            "recovery": None,
            "report_dir": str(report_dir),
        }

    if args.regenerate or not (data_dir / "manifest.json").is_file():
        manifest = generate_all(data_dir, args.seed)
        if not args.json:
            print(
                f"generated seed={manifest['seed']} sessions={len(manifest['sessions'])} "
                f"into {data_dir}"
            )
    if args.reset_state:
        shutil.rmtree(state_dir, ignore_errors=True)
        shutil.rmtree(report_dir, ignore_errors=True)

    recovery = None
    if not args.skip_recovery:
        recovery = run_recovery_drill(data_dir=data_dir, config=config)
        if not recovery.get("passed"):
            raise EngineError(
                E_STATE_INCONSISTENT,
                "forced-termination recovery drill failed",
                {"recovery": recovery},
            )
    pipeline = Pipeline(
        data_dir=data_dir,
        state_dir=state_dir,
        report_dir=report_dir,
        config=config,
    )
    run = pipeline.run(recovery=recovery)
    return run.summary()


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    try:
        summary = _run(args)
    except EngineError as exc:
        if args.json:
            print(json.dumps(exc.to_dict(), ensure_ascii=False, sort_keys=True))
        else:
            print(f"business_code={exc.code}")
            print(f"message={exc.message}")
            if exc.details:
                print(f"details={json.dumps(exc.details, ensure_ascii=False, sort_keys=True)}")
        return 1
    if args.json:
        print(json.dumps(summary, ensure_ascii=False, sort_keys=True))
    else:
        print(f"business_code={summary['business_code']}")
        print(
            "business_codes="
            + ",".join(summary["business_codes"])
        )
        print("stage_versions=" + json.dumps(summary["stage_versions"], sort_keys=True))
        print("stage_reused=" + json.dumps(summary["stage_reused"], sort_keys=True))
        print(
            f"sessions={summary['sessions']} blocks={summary['blocks']} "
            f"drift_events={summary['drift_events']}"
        )
        verification = summary.get("verification") or {}
        print(
            "verification="
            + json.dumps(
                {
                    "dual_precision_consistent": verification.get(
                        "dual_precision_consistent"
                    ),
                    "replay": verification.get("replay"),
                    "serial_whole_consistent": verification.get(
                        "serial_whole_consistent"
                    ),
                    "migration_semantic_checks": verification.get(
                        "migration_semantic_checks"
                    ),
                },
                sort_keys=True,
            )
        )
        if summary.get("recovery") is not None:
            print(
                "recovery="
                + json.dumps(
                    {
                        "passed": summary["recovery"].get("passed"),
                        "business_code": summary["recovery"].get("business_code"),
                    },
                    sort_keys=True,
                )
            )
        print(f"report={Path(args.report_dir) / 'run_report.json'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
