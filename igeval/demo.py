"""End-to-end local demonstration and acceptance evidence generator."""

from __future__ import annotations

import argparse
import dataclasses
import os
import platform
import shutil
import statistics
import sys
from pathlib import Path
from typing import Any

import numpy as np

from .config import EvaluatorConfig
from .engine import QualityEvaluator
from .errors import DomainError
from .samples import build_demo_samples, make_sample
from .snapshot import EvaluationSnapshot, evolve_snapshot
from .util import atomic_write_json, deep_copy_json


PROJECT_ROOT = Path(__file__).resolve().parent.parent


def _percentile(values: list[float], percentile: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    index = min(len(ordered) - 1, max(0, int(round((len(ordered) - 1) * percentile))))
    return round(ordered[index], 6)


def _timing_summary(values: list[float]) -> dict[str, Any]:
    return {
        "count": len(values),
        "min_ms": round(min(values), 6) if values else None,
        "median_ms": round(statistics.median(values), 6) if values else None,
        "p95_ms": _percentile(values, 0.95),
        "max_ms": round(max(values), 6) if values else None,
        "samples_ms": [round(value, 6) for value in values],
    }


def _probe_validation_errors(base_samples, runtime_root: Path) -> dict[str, Any]:
    probes: dict[str, Any] = {}
    config = EvaluatorConfig(workers=1, latency_budget_ms=1_000.0)

    duplicate_source = build_demo_samples(2)
    duplicate = dataclasses.replace(
        duplicate_source[1], sample_id=duplicate_source[0].sample_id
    )
    duplicate_engine = QualityEvaluator(
        [duplicate_source[0], duplicate],
        config=config,
        runtime_dir=runtime_root / "duplicate",
    )
    duplicate_report = duplicate_engine.evaluate_batch()
    probes["duplicate_sample_id"] = {
        "code": duplicate_report["code"],
        "field": duplicate_report["errors"][0]["field"],
        "sample_id": duplicate_report["errors"][0]["sample_id"],
    }

    broken_canvas = dataclasses.replace(
        base_samples[0], width=base_samples[0].width + 2
    )
    canvas_engine = QualityEvaluator(
        [broken_canvas], config=config, runtime_dir=runtime_root / "canvas"
    )
    canvas_report = canvas_engine.evaluate_batch()
    probes["canvas_mismatch"] = {
        "code": canvas_report["code"],
        "field": canvas_report["errors"][0]["field"],
        "stage": canvas_report["errors"][0]["stage"],
        "sample_id": canvas_report["errors"][0]["sample_id"],
    }

    empty_prompt_sample = dataclasses.replace(base_samples[0], prompt="")
    prompt_engine = QualityEvaluator(
        [empty_prompt_sample],
        config=config,
        runtime_dir=runtime_root / "empty-prompt",
    )
    prompt_report = prompt_engine.evaluate_batch()
    probes["empty_prompt"] = {
        "code": prompt_report["code"],
        "field": prompt_report["errors"][0]["field"],
        "sample_id": prompt_report["errors"][0]["sample_id"],
    }

    precision_engine = QualityEvaluator(
        [base_samples[0]],
        config=config,
        runtime_dir=runtime_root / "precision",
    )
    precision_response = precision_engine.query(
        base_samples[0].sample_id, precision="fast-undeclared"
    )
    probes["unknown_precision"] = {
        "code": precision_response["code"],
        "field": precision_response["errors"][0]["field"],
    }
    invalid_budget = precision_engine.query(
        base_samples[0].sample_id, latency_budget_ms=0
    )
    probes["invalid_latency_budget"] = {
        "code": invalid_budget["code"],
        "field": invalid_budget["errors"][0]["field"],
    }
    try:
        EvaluatorConfig(max_feature_memory_bytes=0)
    except DomainError as error:
        probes["invalid_memory_limit"] = {
            "code": error.code,
            "field": error.field,
        }
    return probes


def run_demo(
    *,
    output_path: Path,
    summary_path: Path,
    runtime_dir: Path,
    sample_count: int,
    workers: int,
) -> dict[str, Any]:
    if runtime_dir.exists():
        shutil.rmtree(runtime_dir)
    runtime_dir.mkdir(parents=True, exist_ok=True)

    samples = build_demo_samples(sample_count)
    config = EvaluatorConfig(
        workers=workers,
        latency_budget_ms=100.0,
        max_cache_entries=128,
        max_feature_memory_bytes=8 * 1024 * 1024,
        precision="exact",
    )
    evaluator = QualityEvaluator(
        samples,
        config=config,
        runtime_dir=runtime_dir,
        use_persisted_snapshot=False,
    )

    initial_batch = evaluator.evaluate_batch()
    initial_by_id = {item["sample_id"]: item for item in initial_batch["results"]}

    cold_queries: list[dict[str, Any]] = []
    warm_queries: list[dict[str, Any]] = []
    for sample in samples:
        cold_queries.append(
            evaluator.query(sample.sample_id, latency_budget_ms=100.0)
        )
        warm_queries.append(
            evaluator.query(sample.sample_id, latency_budget_ms=100.0)
        )
    query_consistency = []
    for cold, warm in zip(cold_queries, warm_queries):
        sample_id = cold["sample_id"]
        batch_item = initial_by_id[sample_id]
        query_consistency.append(
            {
                "sample_id": sample_id,
                "batch_score_digest": batch_item["score_digest"],
                "cold_score_digest": cold["score_digest"],
                "warm_score_digest": warm["score_digest"],
                "cold_cache_status": cold["cache"]["status"],
                "warm_cache_status": warm["cache"]["status"],
                "score_equal": (
                    batch_item["score_digest"]
                    == cold["score_digest"]
                    == warm["score_digest"]
                ),
                "stages_equal": all(
                    batch_item["stages"][stage]["score"]
                    == warm["stages"][stage]["score"]
                    for stage in ("preprocess", "semantic", "structure", "quality")
                ),
            }
        )

    initial_self_check = evaluator.self_check(initial_batch)
    initial_snapshot = EvaluationSnapshot.from_dict(
        evaluator.get_snapshot()["snapshot"]
    )
    newer_snapshot = evolve_snapshot(
        initial_snapshot,
        snapshot_version=2,
        normalization_version=2,
        normalization_changes={"mean": 0.60},
    )
    switch_result = evaluator.switch_snapshot(newer_snapshot)
    invalidation_after_switch = evaluator.invalidation_report()
    switched_batch = evaluator.evaluate_batch()
    invalidation_after_switch_batch = evaluator.invalidation_report()

    prompt_target = samples[2].sample_id if len(samples) >= 3 else samples[0].sample_id
    prompt_update = evaluator.update_prompt(
        prompt_target,
        "green forest path with golden sunlight, tall trees, and red flowers",
    )
    prompt_query = evaluator.query(prompt_target, latency_budget_ms=100.0)

    appended_sample = make_sample(
        sample_id=f"sample-{sample_count + 1:03d}",
        seed=9000 + sample_count,
        prompt="abstract teal and amber composition with layered circles",
        reference="balanced layered circles, teal amber palette, clear negative space",
        truth_grade=4,
    )
    append_result = evaluator.append_samples([appended_sample])
    final_batch = evaluator.evaluate_batch()
    final_self_check = evaluator.self_check(final_batch)
    final_invalidation = evaluator.invalidation_report()

    latency_probe = evaluator.query(
        "sample-001",
        latency_budget_ms=1e-9,
        use_cache=False,
    )
    rollback_probe = evaluator.switch_snapshot(initial_snapshot)
    corrupted = deep_copy_json(evaluator.get_snapshot()["snapshot"])
    corrupted["components"]["normalization"]["mean"] = 0.71
    corruption_probe = evaluator.switch_snapshot(corrupted)
    validation_probes = _probe_validation_errors(
        samples[:1], runtime_dir / "validation-probes"
    )

    cold_times = [
        float(item["latency"]["measured_ms"])
        for item in cold_queries
        if item.get("latency")
    ]
    warm_times = [
        float(item["latency"]["measured_ms"])
        for item in warm_queries
        if item.get("latency")
    ]
    all_hits = all(item["cache"]["status"] == "hit" for item in warm_queries)
    all_query_consistent = all(item["score_equal"] for item in query_consistency)
    invalidation_complete = bool(final_invalidation["coverage_ok"])
    worker_pids = initial_batch["execution"]["worker_pids"]
    verification = {
        "batch_ok": initial_batch["status"] == "ok",
        "query_scores_match_batch": all_query_consistent,
        "warm_queries_are_cache_hits": all_hits,
        "initial_self_check_ok": initial_self_check["status"] == "ok",
        "snapshot_switch_ok": switch_result["status"] == "ok",
        "snapshot_switch_invalidated_all": (
            len(invalidation_after_switch["events"][0]["affected_sample_ids"])
            == sample_count
        ),
        "invalidation_coverage_complete": invalidation_complete,
        "final_self_check_ok": final_self_check["status"] == "ok",
        "latency_budget_code_ok": latency_probe["code"] == "E_LATENCY_BUDGET",
        "latency_completed_stages_ok": latency_probe["latency"]["completed_stages"]
        == ["preprocess", "semantic", "structure", "quality"],
        "rollback_code_ok": rollback_probe["code"] == "E_SNAPSHOT_ROLLBACK",
        "corruption_code_ok": corruption_probe["code"] == "E_SNAPSHOT_CORRUPT",
        "process_pool_used": workers > 1 and len(worker_pids) > 1,
    }

    report = {
        "schema_version": "igeval.demo-report/1",
        "environment": {
            "python": sys.version,
            "platform": platform.platform(),
            "numpy": np.__version__,
            "multiprocessing_start_method": "spawn",
            "cpu_count": os.cpu_count(),
        },
        "batch_worker_pids": worker_pids,
        "config": config.to_dict(),
        "controlled_samples": {
            "count": len(samples),
            "canvas": [samples[0].width, samples[0].height],
            "seeds": [sample.seed for sample in samples],
            "truth_grades": [sample.truth_grade for sample in samples],
        },
        "initial_batch": initial_batch,
        "query_latency": {
            "budget_ms": 100.0,
            "cold": _timing_summary(cold_times),
            "warm_cache_hit": _timing_summary(warm_times),
            "estimate_example": cold_queries[0]["latency"]["estimate"],
        },
        "query_consistency": query_consistency,
        "initial_self_check": initial_self_check,
        "snapshot_switch": {
            "result": switch_result,
            "invalidation_before_recompute": invalidation_after_switch,
            "batch_after_switch_summary": {
                "status": switched_batch["status"],
                "snapshot": switched_batch["snapshot"],
                "summary": switched_batch["summary"],
                "ranking": switched_batch["ranking"],
            },
            "invalidation_after_recompute": invalidation_after_switch_batch,
        },
        "prompt_update": {
            "result": prompt_update,
            "query": prompt_query,
        },
        "sample_append": {
            "result": append_result,
            "sample": appended_sample.to_wire(),
        },
        "final_batch": final_batch,
        "final_self_check": final_self_check,
        "final_invalidation": final_invalidation,
        "latency_budget_probe": latency_probe,
        "rollback_probe": rollback_probe,
        "corruption_probe": corruption_probe,
        "validation_probes": validation_probes,
        "verification": verification,
    }
    atomic_write_json(output_path, report)

    status = "PASS" if all(verification.values()) else "FAIL"
    lines = [
        "IGEVAL local deterministic verification",
        f"status: {status}",
        f"python: {sys.version.splitlines()[0]}",
        f"numpy: {np.__version__}",
        f"platform: {platform.platform()}",
        f"workers: {workers} (spawn process pool for batch)",
        f"samples: {sample_count}; final samples: {final_batch['summary']['evaluated']}",
        "",
        "Batch/query consistency",
        f"- initial batch: {initial_batch['status']}",
        f"- query digests match batch: {all_query_consistent}",
        f"- all warm queries cache hits: {all_hits}",
        f"- initial self-check: {initial_self_check['status']}",
        "",
        "Latency observed by local loop",
        f"- cold median/p95 ms: {_timing_summary(cold_times)['median_ms']} / {_timing_summary(cold_times)['p95_ms']}",
        f"- warm median/p95 ms: {_timing_summary(warm_times)['median_ms']} / {_timing_summary(warm_times)['p95_ms']}",
        f"- forced budget code: {latency_probe['code']}",
        f"- completed stages: {', '.join(latency_probe['latency']['completed_stages'])}",
        "",
        "Snapshot and invalidation",
        f"- switch: {switch_result['status']} -> v{switch_result['snapshot']['snapshot_version']}",
        f"- affected before recompute: {len(invalidation_after_switch['events'][0]['affected_sample_ids'])}",
        f"- final coverage complete: {invalidation_complete}",
        f"- rollback probe: {rollback_probe['code']}",
        f"- corruption probe: {corruption_probe['code']}",
        "",
        "Final checks",
        f"- final self-check: {final_self_check['status']} ({final_self_check['code']})",
        f"- final ranking: {', '.join(item['sample_id'] for item in final_batch['ranking'][:5])}",
        f"- report: {output_path}",
    ]
    summary_path.parent.mkdir(parents=True, exist_ok=True)
    summary_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print("\n".join(lines))
    return report


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--output",
        type=Path,
        default=PROJECT_ROOT / "artifacts" / "demo_report.json",
    )
    parser.add_argument(
        "--summary",
        type=Path,
        default=PROJECT_ROOT / "artifacts" / "demo_summary.txt",
    )
    parser.add_argument(
        "--runtime-dir",
        type=Path,
        default=PROJECT_ROOT / "artifacts" / "demo_runtime",
    )
    parser.add_argument("--samples", type=int, default=12)
    parser.add_argument("--workers", type=int, default=4)
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    if args.samples <= 0:
        print("--samples must be positive", file=sys.stderr)
        return 2
    if args.workers <= 0:
        print("--workers must be positive", file=sys.stderr)
        return 2
    report = run_demo(
        output_path=args.output,
        summary_path=args.summary,
        runtime_dir=args.runtime_dir,
        sample_count=args.samples,
        workers=args.workers,
    )
    return 0 if all(report["verification"].values()) else 1


if __name__ == "__main__":
    raise SystemExit(main())

