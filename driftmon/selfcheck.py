"""Cross-module consistency checks used by the UI, CLI, and tests."""

from __future__ import annotations

from typing import Any

from .alerts import AlertStore
from .cache import CacheStore
from .detectors import evaluate_feature_stats
from .engine import RunResult, compare_with_full_rebuild
from .lineage import replay_alert


def run_selfcheck(
    result: RunResult,
    cache: CacheStore,
    alert_store: AlertStore,
    incremental_report: dict[str, Any] | None = None,
) -> dict[str, Any]:
    checks: list[dict[str, Any]] = []

    report = incremental_report or {}
    affected = {
        tuple(item)
        for item in report.get("affected_products", result.affected_products)
    }
    recomputed = {
        tuple(item)
        for item in report.get("recomputed_products", result.recomputed_products)
    }
    reused = {
        tuple(item) for item in report.get("reused_products", result.reused_products)
    }
    active_identities = {
        (product.feature, product.target_batch) for product in result.products
    }
    intersection = recomputed & reused
    checks.append(
        {
            "name": "recompute_reuse_disjoint",
            "passed": not intersection,
            "details": {"intersection": [list(item) for item in sorted(intersection)]},
        }
    )
    checks.append(
        {
            "name": "affected_coverage",
            "passed": affected.issubset(recomputed | reused)
            and (recomputed | reused).issubset(active_identities),
            "details": {
                "affected_count": len(affected),
                "recomputed_count": len(recomputed),
                "reused_count": len(reused),
                "uncovered": [
                    list(item) for item in sorted(affected - (recomputed | reused))
                ],
                "non_active": [
                    list(item)
                    for item in sorted((recomputed | reused) - active_identities)
                ],
            },
        }
    )

    decision_mismatches = []
    feature_map = result.config.feature_index()
    for product in result.products:
        feature = feature_map[product.feature]
        recomputed_test = evaluate_feature_stats(
            result.config, feature, product.stats_content
        )
        if recomputed_test != product.test_content:
            decision_mismatches.append([product.feature, product.target_batch])
    checks.append(
        {
            "name": "decision_recomputable_from_stats",
            "passed": not decision_mismatches,
            "details": {"mismatches": decision_mismatches},
        }
    )

    key_mismatches = []
    for product in result.products:
        if cache.key_for(cache.explain(product.stats_key)["key_components"]) != product.stats_key:
            key_mismatches.append([product.feature, product.target_batch, "stats"])
        if cache.key_for(cache.explain(product.test_key)["key_components"]) != product.test_key:
            key_mismatches.append([product.feature, product.target_batch, "test"])
    checks.append(
        {
            "name": "cache_keys_explainable",
            "passed": not key_mismatches,
            "details": {"mismatches": key_mismatches},
        }
    )

    try:
        full_comparison = compare_with_full_rebuild(result, cache)
        full_passed = bool(full_comparison["match"])
    except Exception as exc:
        full_comparison = {"match": False, "error": str(exc)}
        full_passed = False
    checks.append(
        {
            "name": "incremental_equals_full_rebuild",
            "passed": full_passed,
            "details": full_comparison,
        }
    )

    replay_results = []
    replay_failures = []
    for alert in alert_store.list_alerts():
        try:
            replay = replay_alert(alert, cache)
            replay_results.append(
                {
                    "alert_id": replay["alert_id"],
                    "matches": replay["matches"],
                    "replayed_checksum": replay["replayed_checksum"],
                }
            )
        except Exception as exc:
            replay_failures.append(
                {"alert_id": alert.get("alert_id"), "error": str(exc)}
            )
    checks.append(
        {
            "name": "alerts_replay_from_lineage",
            "passed": not replay_failures,
            "details": {
                "replayed": replay_results,
                "failures": replay_failures,
            },
        }
    )

    return {
        "ok": all(check["passed"] for check in checks),
        "checks": checks,
        "latest_recomputed": [list(item) for item in sorted(recomputed)],
        "latest_reused": [list(item) for item in sorted(reused)],
        "latest_affected": [list(item) for item in sorted(affected)],
    }

