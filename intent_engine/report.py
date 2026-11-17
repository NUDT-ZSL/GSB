"""Deterministic JSON report assembly."""

from __future__ import annotations

import math
from typing import Any, Iterable

from .constants import SCHEMA_VERSION, STAGE_ORDER, STAGES
from .utils import quantize, stable_hash


def _failure_key(item: dict[str, Any]) -> tuple[str, int, str, str]:
    return (
        str(item.get("sample_id", "")),
        STAGE_ORDER.get(str(item.get("stage", "")), 99),
        str(item.get("business_code", "")),
        str(item.get("exception_type", "")),
    )


def _confidence_stats(results: Iterable[dict[str, Any]], threshold: float, digits: int) -> dict[str, Any]:
    values = [
        float(result["confidence"])
        for result in results
        if result.get("status") == "ok" and isinstance(result.get("confidence"), (int, float))
    ]
    if not values:
        return {
            "count": 0,
            "min": None,
            "max": None,
            "mean": None,
            "threshold": quantize(threshold, digits),
            "threshold_pass_count": 0,
        }
    return {
        "count": len(values),
        "min": quantize(min(values), digits),
        "max": quantize(max(values), digits),
        "mean": quantize(math.fsum(sorted(values)) / len(values), digits),
        "threshold": quantize(threshold, digits),
        "threshold_pass_count": sum(1 for value in values if value >= threshold),
    }


def build_report(
    *,
    results: Iterable[dict[str, Any]],
    loader_failures: Iterable[dict[str, Any]],
    stage_metrics: dict[str, dict[str, Any]],
    incremental: dict[str, Any],
    resources: Any,
    injections: Any,
    snapshot_version: int,
    interrupted: bool,
    complete: bool,
    corpus_path: str,
    config_path: str,
    injection_path: str | None,
) -> dict[str, Any]:
    ordered_results = sorted(results, key=lambda item: str(item.get("sample_id", "")))
    success_results = [item for item in ordered_results if item.get("status") == "ok"]
    failures: list[dict[str, Any]] = [dict(item) for item in loader_failures]
    for result in ordered_results:
        if result.get("status") != "ok" and isinstance(result.get("error"), dict):
            error = dict(result["error"])
            error.setdefault("injection_ids", result.get("injection_ids", []))
            failures.append(error)
    failures.sort(key=_failure_key)
    intent_distribution: dict[str, int] = {}
    for result in success_results:
        name = str(result.get("intent", ""))
        intent_distribution[name] = intent_distribution.get(name, 0) + 1
    result_by_sample = {str(result.get("sample_id")): result for result in ordered_results}
    injection_hits: list[dict[str, Any]] = []
    for injection in injections.injections:
        result = result_by_sample.get(injection.sample_id)
        if result is None:
            continue
        hit_ids = set(result.get("injection_ids", []))
        if injection.injection_id not in hit_ids:
            continue
        injection_hits.append(
            {
                "injection_id": injection.injection_id,
                "sample_id": injection.sample_id,
                "stage": injection.stage,
                "fault": injection.fault,
                "outcome": result.get("status", "unknown"),
                "business_code": (
                    result.get("error", {}).get("business_code")
                    if isinstance(result.get("error"), dict)
                    else None
                ),
            }
        )
    injection_hits.sort(key=lambda item: (item["sample_id"], STAGE_ORDER.get(item["stage"], 99), item["injection_id"]))
    by_stage: dict[str, dict[str, Any]] = {}
    total_hits = 0
    total_misses = 0
    total_invalidations = 0
    for stage in STAGES:
        metrics = stage_metrics.get(stage, {})
        hits = int(metrics.get("cache_hits", 0))
        misses = int(metrics.get("computed", 0))
        invalidated = int(incremental.get("invalidated_stage_counts", {}).get(stage, 0))
        total_hits += hits
        total_misses += misses
        total_invalidations += invalidated
        by_stage[stage] = {
            "computed": misses,
            "cache_hits": hits,
            "invalidated": invalidated,
            "failures": int(metrics.get("failures", 0)),
            "duration_ms": quantize(float(metrics.get("duration_ms", 0.0)), 3),
        }
    task_sort_keys = [
        f"{result.get('sample_id')}:{stage}"
        for result in ordered_results
        for stage in STAGES
    ]
    report = {
        "schema_version": SCHEMA_VERSION,
        "status": "ok" if complete else "interrupted",
        "complete": bool(complete),
        "interrupted": bool(interrupted),
        "corpus_path": str(corpus_path),
        "config_path": str(config_path),
        "injection_path": str(injection_path) if injection_path is not None else None,
        "config_hash": resources.config.config_hash,
        "resource_hash": resources.resource_hash,
        "injection_hash": injections.config_hash,
        "snapshot_version": snapshot_version,
        "sample_count": len(ordered_results),
        "success_count": len(success_results),
        "failure_count": len(failures),
        "intent_distribution": dict(sorted(intent_distribution.items())),
        "confidence_stats": _confidence_stats(success_results, resources.config.confidence_threshold, resources.config.digits),
        "stage_timing_mode": "wall_clock_non_deterministic",
        "stage_timings_ms": {
            stage: quantize(float(stage_metrics.get(stage, {}).get("duration_ms", 0.0)), 3)
            for stage in STAGES
        },
        "cache": {
            "hit_count": total_hits,
            "miss_count": total_misses,
            "invalidation_count": total_invalidations,
            "cold_sample_count": len(incremental.get("cold_samples", [])),
            "affected_sample_count": len(incremental.get("affected_samples", [])),
            "by_stage": by_stage,
            "changed_entries": incremental.get("resource_diff", {}),
            "changed_config": incremental.get("config_diff", {}),
            "reference_counts": incremental.get("reference_counts", {}),
            "reference_digest": incremental.get("reference_digest", ""),
        },
        "incremental": {
            "cold_samples": incremental.get("cold_samples", []),
            "affected_samples": incremental.get("affected_samples", []),
            "invalidated_stages": incremental.get("invalidated_stages", {}),
            "reasons": incremental.get("reasons", {}),
        },
        "injection_hits": injection_hits,
        "injection_hit_ids": [item["injection_id"] for item in injection_hits],
        "task_sort_keys": task_sort_keys,
        "failures": failures,
        "results": ordered_results,
    }
    report["stage_timings"] = dict(report["stage_timings_ms"])
    report["cache_hits"] = total_hits
    report["cache_invalidations"] = total_invalidations
    report["report_fingerprint"] = stable_hash(
        {
            key: value
            for key, value in report.items()
            if key not in {"stage_timings_ms", "cache"}
        }
    )
    return report


def config_mismatch_report(
    *,
    error: Any,
    corpus_path: str,
    config_path: str,
    injection_path: str | None,
    snapshot_version: int,
) -> dict[str, Any]:
    details = dict(error.details)
    failure = {
        "sample_id": "__snapshot__",
        "line_number": 0,
        "stage": "startup",
        "business_code": "E_CONFIG_MISMATCH",
        "exception_type": type(error).__name__,
        "message": str(error),
        "details": details,
        "injection_ids": [],
    }
    report = {
        "schema_version": SCHEMA_VERSION,
        "status": "error",
        "complete": False,
        "interrupted": False,
        "corpus_path": str(corpus_path),
        "config_path": str(config_path),
        "injection_path": str(injection_path) if injection_path is not None else None,
        "snapshot_version": snapshot_version,
        "sample_count": 0,
        "success_count": 0,
        "failure_count": 1,
        "intent_distribution": {},
        "confidence_stats": {
            "count": 0,
            "min": None,
            "max": None,
            "mean": None,
            "threshold": None,
            "threshold_pass_count": 0,
        },
        "stage_timings_ms": {stage: 0.0 for stage in STAGES},
        "cache": {
            "hit_count": 0,
            "miss_count": 0,
            "invalidation_count": 0,
            "by_stage": {},
        },
        "injection_hits": [],
        "task_sort_keys": [],
        "failures": [failure],
        "results": [],
    }
    report["report_fingerprint"] = stable_hash(report)
    return report
