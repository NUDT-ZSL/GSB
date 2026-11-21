"""Cross-module consistency and invalidation coverage self-check."""

from __future__ import annotations

from typing import Any

from .errors import E_INCONSISTENT
from .pipeline import STAGE_ORDER


def _failure(
    category: str,
    *,
    sample_id: str | None = None,
    stage: str | None = None,
    details: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        "code": E_INCONSISTENT,
        "category": category,
        "sample_id": sample_id,
        "stage": stage,
        "details": details or {},
    }


def _compare_stages(
    expected: dict[str, Any],
    actual: dict[str, Any],
    tolerance: float,
    sample_id: str,
) -> dict[str, Any] | None:
    for stage in STAGE_ORDER:
        expected_value = expected.get("stages", {}).get(stage, {}).get("score")
        actual_value = actual.get("stages", {}).get(stage, {}).get("score")
        if expected_value is None or actual_value is None:
            return _failure(
                "missing_stage",
                sample_id=sample_id,
                stage=stage,
                details={"expected": expected_value, "actual": actual_value},
            )
        if abs(float(expected_value) - float(actual_value)) > tolerance:
            return _failure(
                "score_mismatch",
                sample_id=sample_id,
                stage=stage,
                details={
                    "precision_tolerance": tolerance,
                    "batch": expected_value,
                    "query": actual_value,
                },
            )
    if abs(float(expected["final_score"]) - float(actual["final_score"])) > tolerance:
        return _failure(
            "score_mismatch",
            sample_id=sample_id,
            stage="quality",
            details={
                "precision_tolerance": tolerance,
                "batch": expected["final_score"],
                "query": actual["final_score"],
            },
        )
    return None


def run_self_check(
    evaluator: Any,
    batch_report: dict[str, Any] | None = None,
) -> dict[str, Any]:
    batch = batch_report if batch_report is not None else evaluator.evaluate_batch()
    tolerance = evaluator.config.consistency_tolerance
    failures: list[dict[str, Any]] = []
    checks: dict[str, Any] = {
        "stage_sample_sets": {},
        "score_ranges": {},
        "aggregation_order": None,
        "batch_query_consistency": {},
        "cache_key_feature_digest": {},
        "invalidation_coverage": {},
    }

    results = list(batch.get("results", []))
    successful_ids = {item["sample_id"] for item in results}
    if batch.get("status") == "error" and not results:
        failures.append(
            _failure(
                "batch_status",
                details={
                    "status": batch.get("status"),
                    "code": batch.get("code"),
                    "errors": batch.get("errors", []),
                },
            )
        )

    for stage in STAGE_ORDER:
        stage_ids = set(batch.get("stage_sample_ids", {}).get(stage, []))
        check = {
            "expected_sample_ids": sorted(successful_ids),
            "actual_sample_ids": sorted(stage_ids),
            "ok": stage_ids == successful_ids,
        }
        checks["stage_sample_sets"][stage] = check
        if not check["ok"]:
            failures.append(
                _failure(
                    "stage_sample_set",
                    stage=stage,
                    details={
                        "missing": sorted(successful_ids - stage_ids),
                        "extra": sorted(stage_ids - successful_ids),
                    },
                )
            )

    for item in results:
        sample_id = item["sample_id"]
        for stage in STAGE_ORDER:
            score = float(item["stages"][stage]["score"])
            ok = 0.0 <= score <= 1.0
            checks["score_ranges"][f"{sample_id}:{stage}"] = {
                "score": score,
                "ok": ok,
            }
            if not ok:
                failures.append(
                    _failure(
                        "score_range",
                        sample_id=sample_id,
                        stage=stage,
                        details={"score": score},
                    )
                )
        final_score = float(item["final_score"])
        ok = 0.0 <= final_score <= 1.0
        checks["score_ranges"][f"{sample_id}:final"] = {
            "score": final_score,
            "ok": ok,
        }
        if not ok:
            failures.append(
                _failure(
                    "score_range",
                    sample_id=sample_id,
                    stage="quality",
                    details={"score": final_score},
                )
            )

    expected_order = [
        item["sample_id"]
        for item in sorted(
            results, key=lambda item: (-float(item["final_score"]), item["sample_id"])
        )
    ]
    actual_order = [item["sample_id"] for item in batch.get("ranking", [])]
    ranks_ok = all(
        int(item["rank"]) == index
        for index, item in enumerate(batch.get("ranking", []), start=1)
    )
    checks["aggregation_order"] = {
        "expected": expected_order,
        "actual": actual_order,
        "ok": expected_order == actual_order and ranks_ok,
    }
    if not checks["aggregation_order"]["ok"]:
        failures.append(
            _failure(
                "aggregation_order",
                details=checks["aggregation_order"],
            )
        )

    for item in results:
        sample_id = item["sample_id"]
        query_budget = 1_000_000_000.0
        warm = evaluator.query(
            sample_id,
            latency_budget_ms=query_budget,
            use_cache=True,
        )
        cold = evaluator.query(
            sample_id,
            latency_budget_ms=query_budget,
            use_cache=False,
        )
        stage_failure = _compare_stages(item, cold, tolerance, sample_id)
        if stage_failure is not None:
            failures.append(stage_failure)
        warm_stage_failure = _compare_stages(cold, warm, tolerance, sample_id)
        if warm_stage_failure is not None:
            failures.append(warm_stage_failure)
        digest_ok = (
            item["feature_digest"] == cold["feature_digest"] == warm["feature_digest"]
            and item["score_digest"] == cold["score_digest"] == warm["score_digest"]
            and item["cache_key"] == cold["cache"]["key"] == warm["cache"]["key"]
        )
        checks["batch_query_consistency"][sample_id] = {
            "batch_score_digest": item["score_digest"],
            "cold_score_digest": cold["score_digest"],
            "warm_score_digest": warm["score_digest"],
            "warm_cache_status": warm.get("cache", {}).get("status"),
            "ok": stage_failure is None and warm_stage_failure is None and digest_ok,
        }
        if not digest_ok:
            failures.append(
                _failure(
                    "digest_mismatch",
                    sample_id=sample_id,
                    details={
                        "batch_feature_digest": item["feature_digest"],
                        "query_feature_digest": cold["feature_digest"],
                        "batch_score_digest": item["score_digest"],
                        "query_score_digest": cold["score_digest"],
                        "batch_cache_key": item["cache_key"],
                        "query_cache_key": cold["cache"]["key"],
                    },
                )
            )

        entry_report = evaluator.cache_entry_report(sample_id)
        warm_cache_status = warm.get("cache", {}).get("status")
        expected_entry = warm_cache_status in {"hit", "stored"}
        entry = entry_report.get("entry")
        entry_ok = (
            entry is not None
            and entry["cache_key"] == cold["cache"]["key"]
            and entry["feature_digest"] == cold["feature_digest"]
            and entry["score_digest"] == cold["score_digest"]
            and entry["image_content_hash"] == cold["input"]["image_content_hash"]
            and entry["text_term_digest"] == cold["input"]["text_term_digest"]
            and entry["snapshot_id"] == cold["snapshot"]["snapshot_id"]
        )
        checks["cache_key_feature_digest"][sample_id] = {
            "expected_entry": expected_entry,
            "cache_key": cold["cache"]["key"],
            "feature_digest": cold["feature_digest"],
            "entry": entry,
            "ok": entry_ok if expected_entry else True,
        }
        if expected_entry and not entry_ok:
            failures.append(
                _failure(
                    "cache_key_feature_digest",
                    sample_id=sample_id,
                    details=checks["cache_key_feature_digest"][sample_id],
                )
            )
        if item["snapshot"]["snapshot_id"] != cold["snapshot"]["snapshot_id"]:
            failures.append(
                _failure(
                    "snapshot_mismatch",
                    sample_id=sample_id,
                    details={
                        "batch": item["snapshot"]["snapshot_id"],
                        "query": cold["snapshot"]["snapshot_id"],
                    },
                )
            )

    invalidation = evaluator.invalidation_report()
    for event in invalidation.get("events", []):
        affected = set(event["affected_sample_ids"])
        hits = set(event["cache_hit_sample_ids"])
        recomputed = set(event["recomputed_sample_ids"])
        coverage_ok = not (hits & recomputed) and (hits | recomputed) == affected
        event_ok = coverage_ok
        checks["invalidation_coverage"][str(event["event_id"])] = {
            "event_id": event["event_id"],
            "kind": event["kind"],
            "affected_sample_ids": sorted(affected),
            "cache_hit_sample_ids": sorted(hits),
            "recomputed_sample_ids": sorted(recomputed),
            "intersection": sorted(hits & recomputed),
            "uncovered": sorted(affected - (hits | recomputed)),
            "ok": event_ok,
        }
        if not event_ok:
            failures.append(
                _failure(
                    "invalidation_coverage",
                    details=checks["invalidation_coverage"][str(event["event_id"])],
                )
            )

    cache_stats = evaluator.cache.stats()
    if failures:
        first = failures[0]
        return {
            "schema_version": "igeval.self-check/1",
            "status": "error",
            "code": E_INCONSISTENT,
            "category": first["category"],
            "sample_id": first["sample_id"],
            "stage": first["stage"],
            "failure_count": len(failures),
            "failures": failures,
            "checks": checks,
            "batch_snapshot": batch.get("snapshot"),
            "cache": cache_stats,
        }
    return {
        "schema_version": "igeval.self-check/1",
        "status": "ok",
        "code": None,
        "category": None,
        "sample_id": None,
        "stage": None,
        "failure_count": 0,
        "failures": [],
        "checks": checks,
        "batch_snapshot": batch.get("snapshot"),
        "cache": cache_stats,
    }

