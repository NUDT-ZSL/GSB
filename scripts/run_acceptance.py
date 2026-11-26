#!/usr/bin/env python3
from __future__ import annotations

import argparse
import copy
import json
import platform
import shutil
import sqlite3
import sys
from pathlib import Path
from typing import Any

from dsn_workbench.config import ConfigBundle, read_config, write_v1
from dsn_workbench.pipeline import (
    PipelineOptions,
    SimulatedCrash,
    run_pipeline,
)
from dsn_workbench.report import write_diagnostic_report
from dsn_workbench.sample import generate_sample
from dsn_workbench.util import atomic_write_json, atomic_write_text


def write_outputs(path: Path, outputs: list[dict[str, Any]]) -> None:
    atomic_write_text(
        path,
        "".join(
            json.dumps(output, ensure_ascii=False, sort_keys=True) + "\n"
            for output in outputs
        ),
    )


def prune_runtime_caches(root: Path) -> None:
    """Keep the evidence ledger/checkpoints but remove bulky cache payloads."""
    for name in ("serial", "parallel", "v2", "recovery"):
        runtime = root / name
        cache_dir = runtime / "cache"
        if cache_dir.exists():
            shutil.rmtree(cache_dir)
        snapshots = runtime / "state" / "config_snapshots"
        if snapshots.exists():
            for snapshot in snapshots.glob("*.json"):
                if snapshot.name != "current.json":
                    snapshot.unlink()


def run() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", default="artifacts/acceptance")
    parser.add_argument("--seed", type=int, default=20261127)
    parser.add_argument("--count", type=int, default=24)
    args = parser.parse_args()

    root = Path(args.output).resolve()
    if root.exists():
        shutil.rmtree(root)
    root.mkdir(parents=True)
    sample_dir = root / "sample"
    sample = generate_sample(sample_dir, seed=args.seed, count=args.count)
    v1 = Path(sample["v1_config"])
    v2 = Path(sample["v2_config"])
    tasks = Path(sample["tasks"])

    serial_root = root / "serial"
    serial = run_pipeline(
        v1,
        tasks,
        cache_dir=serial_root / "cache",
        state_dir=serial_root / "state",
        options=PipelineOptions(workers_override=1, batch_size_override=5),
    )
    parallel_root = root / "parallel"
    parallel = run_pipeline(
        v1,
        tasks,
        cache_dir=parallel_root / "cache",
        state_dir=parallel_root / "state",
        options=PipelineOptions(workers_override=3, batch_size_override=3),
    )
    write_outputs(root / "parallel_output.jsonl", parallel.outputs)

    v2_root = root / "v2"
    v2_run = run_pipeline(
        v2,
        tasks,
        cache_dir=v2_root / "cache",
        state_dir=v2_root / "state",
        options=PipelineOptions(workers_override=2, batch_size_override=4),
    )

    baseline_bundle = read_config(v1)
    changed_data = copy.deepcopy(baseline_bundle.data)
    changed_data["dictionary"] = [
        entry
        for entry in changed_data["dictionary"]
        if entry["term"] != "财报"
    ]
    changed_data["dictionary_version"] = "lex-2026-11-27.2"
    changed_data["generation"] = 2
    changed_data["logical_clock_ms"] += 1000
    changed_path = root / "changed-v1.json"
    write_v1(
        ConfigBundle(
            data=changed_data,
            format_version=1,
            source="acceptance:changed",
            provenance=baseline_bundle.provenance,
        ),
        changed_path,
    )
    invalidated = run_pipeline(
        changed_path,
        tasks,
        cache_dir=parallel_root / "cache",
        state_dir=parallel_root / "state",
        options=PipelineOptions(workers_override=3, batch_size_override=3),
    )

    crash_root = root / "recovery"
    interrupted = None
    try:
        run_pipeline(
            v1,
            tasks,
            cache_dir=crash_root / "cache",
            state_dir=crash_root / "state",
            options=PipelineOptions(
                workers_override=3,
                batch_size_override=3,
                crash_stage="disambiguation",
                crash_after_batches=1,
            ),
        )
    except SimulatedCrash as exc:
        interrupted = {
            "stage": exc.stage,
            "batches_completed": exc.batches_completed,
        }
        crash_report_path = crash_root / "state" / "last_run.json"
        if crash_report_path.exists():
            shutil.copyfile(crash_report_path, root / "interrupted_run.json")
    recovered = run_pipeline(
        v1,
        tasks,
        cache_dir=crash_root / "cache",
        state_dir=crash_root / "state",
        options=PipelineOptions(workers_override=3, batch_size_override=3),
    )
    write_outputs(root / "recovered_output.jsonl", recovered.outputs)

    parallel_ok = (
        serial.report["output_sha256"] == parallel.report["output_sha256"]
        and parallel.report["parallel_verification"]["status"] == "verified"
        and parallel.report["parallel_verification"]["first_difference"] is None
    )
    v2_ok = serial.report["output_sha256"] == v2_run.report["output_sha256"]
    invalidation_plan = invalidated.report["cache_plan"]
    invalidation_ok = (
        len(invalidation_plan["affected_sentences"]) > 0
        and len(invalidation_plan["reuse_sentences"]) > 0
        and len(invalidation_plan["affected_sentences"])
        + len(invalidation_plan["reuse_sentences"])
        == args.count
    )
    stage_map = {
        stage["stage"]: stage for stage in recovered.report["stage_reports"]
    }
    recovery_ok = (
        stage_map["normalize"]["checkpoint_reused"]
        and stage_map["candidates"]["checkpoint_reused"]
        and not stage_map["disambiguation"]["checkpoint_reused"]
        and stage_map["disambiguation"]["computed"] == args.count
        and len(recovered.report["pending_discarded"]) > 0
        and recovered.report["output_sha256"]
        == parallel.report["output_sha256"]
    )
    all_ok = bool(parallel_ok and v2_ok and invalidation_ok and recovery_ok)

    verification = {
        "ok": all_ok,
        "environment": {
            "python": platform.python_version(),
            "sqlite": sqlite3.sqlite_version,
            "platform": platform.platform(),
        },
        "seed": args.seed,
        "sentence_count": args.count,
        "serial_parallel": {
            "ok": parallel_ok,
            "serial_output_sha256": serial.report["output_sha256"],
            "parallel_output_sha256": parallel.report["output_sha256"],
            "verification": parallel.report["parallel_verification"],
            "reference_metrics": parallel.report["reference_metrics"],
        },
        "v2_compatibility": {
            "ok": v2_ok,
            "v1_output_sha256": serial.report["output_sha256"],
            "v2_output_sha256": v2_run.report["output_sha256"],
        },
        "targeted_invalidation": {
            "ok": invalidation_ok,
            "changed_terms": invalidation_plan["changed_terms"],
            "affected_sentences": invalidation_plan["affected_sentences"],
            "reuse_sentences": invalidation_plan["reuse_sentences"],
            "reused_stages": invalidation_plan["reused_stages"],
            "stage_reports": invalidated.report["stage_reports"],
        },
        "interrupt_recovery": {
            "ok": recovery_ok,
            "interrupted": interrupted,
            "pending_discarded": recovered.report["pending_discarded"],
            "stage_reports": recovered.report["stage_reports"],
            "checkpoint_versions": {
                stage: checkpoint["version"]
                for stage, checkpoint in recovered.report["checkpoints"].items()
            },
            "recovered_output_sha256": recovered.report["output_sha256"],
            "clean_parallel_output_sha256": parallel.report["output_sha256"],
        },
        "state_paths": {
            "parallel": str(parallel_root / "state"),
            "recovery": str(crash_root / "state"),
        },
    }
    atomic_write_json(root / "verification.json", verification)
    write_diagnostic_report(
        parallel_root / "state",
        root / "diagnostic.html",
        report=parallel.report,
        outputs=parallel.outputs,
        title="Acceptance Diagnostics - Deterministic Segmentation",
    )
    prune_runtime_caches(root)
    print(json.dumps(verification, ensure_ascii=False, indent=2, sort_keys=True))
    return 0 if all_ok else 1


if __name__ == "__main__":
    raise SystemExit(run())

