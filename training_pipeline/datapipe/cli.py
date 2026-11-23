"""Repeatable command-line interface.

Subcommands:
  load      parse + validate all JSON inputs (writes nothing persistent)
  schedule  run cache warm-up and all prefetch rounds
  assemble  assemble batches from committed shards and verify against truth
  resume    reconcile an interrupted run, then continue to completion
  status    stable-sorted JSON of watermarks / hits / abandons / budget
  verify    full end-to-end run plus a machine-readable verification report
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from typing import Any, Dict

from .assembler import Assembler, serial_truth_batches, verify_coverage, compare_with_truth
from .errors import PipelineError
from .injection import ForcedCrash
from .loader import load_scenario
from .pipeline import Pipeline, plan_rounds
from .sharding import build_shard_manifest


def _emit(payload: Dict[str, Any]) -> None:
    print(json.dumps(payload, indent=2, sort_keys=True, ensure_ascii=False))


def _report_round(report: Any) -> Dict[str, Any]:
    return {
        "round_index": report.round_index,
        "started_clock_ms": report.started_clock_ms,
        "ended_clock_ms": report.ended_clock_ms,
        "timed_out": report.timed_out,
        "counts": report.counts(),
        "budget_used_bytes": report.budget_used_bytes,
        "shards": [
            {
                "target": result.candidate.key,
                "estimated_bytes": result.candidate.estimated_bytes,
                "started": result.started,
                "outcome": result.outcome,
                "reason": result.reason,
                "cache_hit": result.cache_hit,
                "error_code": result.error_code,
            }
            for result in report.results
        ],
    }


def cmd_load(args: argparse.Namespace) -> int:
    scenario = load_scenario(args.input_dir)
    _emit(
        {
            "status": "valid",
            "datasets": [spec.name for spec in scenario.datasets],
            "job": scenario.job.name,
            "cache_view_entries": len(scenario.cache_view),
            "clock_steps": len(scenario.clock_steps),
            "injections": len(scenario.injections),
            "planned_rounds": len(plan_rounds(scenario)),
        }
    )
    return 0


def cmd_schedule(args: argparse.Namespace) -> int:
    scenario = load_scenario(args.input_dir)
    pipeline = Pipeline(args.work_dir, scenario)
    try:
        result = pipeline.run(assemble=False)
        _emit(
            {
                "invalidation": result.invalidation,
                "rounds": [_report_round(report) for report in result.round_reports],
                "recovered": result.recovered,
                "recovery_detail": result.recovery_detail,
                "clock": result.clock,
                "status": pipeline.status(),
            }
        )
        return 0
    finally:
        pipeline.close()


def cmd_assemble(args: argparse.Namespace) -> int:
    scenario = load_scenario(args.input_dir)
    pipeline = Pipeline(args.work_dir, scenario)
    try:
        assembler = Assembler(scenario, pipeline.store, pipeline.injector)
        batches = assembler.assemble()
        _emit(
            {
                "batches": len(batches),
                "first_batch": {
                    "epoch": batches[0].epoch,
                    "digest": batches[0].digest,
                    "sample_count": len(batches[0].sample_identities),
                    "sources": list(dict.fromkeys(batches[0].sources)),
                },
                "status": pipeline.status(),
            }
        )
        return 0
    finally:
        pipeline.close()


def cmd_resume(args: argparse.Namespace) -> int:
    scenario = load_scenario(args.input_dir)
    pipeline = Pipeline(args.work_dir, scenario)
    try:
        from .pipeline import plan_rounds

        open_row = pipeline.store.get_open_round()
        offset = pipeline.round_offset()
        before = (
            pipeline._resume_state(
                plan_rounds(scenario)[open_row["round_index"] - offset], open_row["round_index"]
            )
            if open_row is not None and offset <= open_row["round_index"] < offset + len(plan_rounds(scenario))
            else {"open_round": None, "replayed_missing": [], "already_committed": []}
        )
        result = pipeline.run()
        _emit(
            {
                "resume_before": before,
                "recovered": result.recovered,
                "recovery_detail": result.recovery_detail,
                "rounds": [_report_round(report) for report in result.round_reports],
                "batches": len(result.batches),
                "clock": result.clock,
                "status": pipeline.status(),
            }
        )
        return 0
    finally:
        pipeline.close()


def cmd_status(args: argparse.Namespace) -> int:
    scenario = load_scenario(args.input_dir)
    pipeline = Pipeline(args.work_dir, scenario)
    try:
        _emit(pipeline.status())
        return 0
    finally:
        pipeline.close()


def cmd_verify(args: argparse.Namespace) -> int:
    scenario = load_scenario(args.input_dir)
    pipeline = Pipeline(args.work_dir, scenario)
    try:
        result = pipeline.run()
        truth = serial_truth_batches(scenario)
        compare_with_truth(scenario, result.batches)
        verify_coverage(scenario, result.batches)
        # Determinism re-derivation check: rebuild every manifest twice.
        determinism = []
        for spec in scenario.datasets:
            first = build_shard_manifest(spec, 0)
            second = build_shard_manifest(spec, 0)
            determinism.append(
                {"dataset": spec.name, "stable": first.digest == second.digest
                 and first.byte_size == second.byte_size}
            )
        report = {
            "status": "verified",
            "invalidation": result.invalidation,
            "rounds": [_report_round(report) for report in result.round_reports],
            "batch_truth": {
                "truth_batches": len(truth),
                "actual_batches": len(result.batches),
                "all_digests_match": [b.digest for b in truth] == [b.digest for b in result.batches],
            },
            "determinism": determinism,
            "fired_injections": pipeline.injector.fired,
            "clock": result.clock,
            "status": pipeline.status(),
        }
        if args.report:
            with open(args.report, "w", encoding="utf-8") as handle:
                json.dump(report, handle, indent=2, sort_keys=True, ensure_ascii=False)
        _emit(report)
        return 0
    finally:
        pipeline.close()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="datapipe", description="Deterministic local training data pipeline")
    parser.add_argument("--input-dir", required=True, help="directory containing the five JSON inputs")
    parser.add_argument("--work-dir", required=True, help="writable directory for cache + SQLite")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("load", help="validate inputs without persisting anything")
    sub.add_parser("schedule", help="warm cache and run prefetch rounds")
    sub.add_parser("assemble", help="assemble batches from committed shards")
    sub.add_parser("resume", help="recover an interrupted run and continue")
    sub.add_parser("status", help="stable JSON status report")
    verify_parser = sub.add_parser("verify", help="full run plus verification report")
    verify_parser.add_argument("--report", help="optional path to write the JSON report")
    return parser


def main(argv: Any = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    handlers = {
        "load": cmd_load,
        "schedule": cmd_schedule,
        "assemble": cmd_assemble,
        "resume": cmd_resume,
        "status": cmd_status,
        "verify": cmd_verify,
    }
    try:
        return handlers[args.command](args)
    except ForcedCrash as exc:
        _emit(
            {
                "status": "interrupted",
                "error_code": "E6001_FORCED_CRASH",
                "message": str(exc),
                "diagnostics": {"stage": exc.stage, "target": exc.target},
            }
        )
        return 3
    except PipelineError as exc:
        _emit(
            {
                "status": "error",
                "error_code": exc.code,
                "message": exc.message,
                "diagnostics": exc.diagnostics,
            }
        )
        return 2


if __name__ == "__main__":
    sys.exit(main())
