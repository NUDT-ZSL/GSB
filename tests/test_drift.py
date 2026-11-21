from __future__ import annotations

import pytest

from driftmon.cache import CacheStore
from driftmon.config import default_config
from driftmon.detectors import (
    SEVERITY_HIGH,
    SEVERITY_NONE,
    evaluate_feature_stats,
    rank_test_results,
    severity_for,
)
from driftmon.engine import MonitorEngine
from driftmon.models import QUANTILE_DISTANCE


def test_decision_can_be_recomputed_from_stats(runtime) -> None:
    config, cache, alerts = runtime
    result = MonitorEngine(config, cache, alerts).run(3)
    feature_map = config.feature_index()
    for product in result.products:
        recomputed = evaluate_feature_stats(
            config, feature_map[product.feature], product.stats_content
        )
        assert recomputed == product.test_content


def test_cross_feature_ties_break_by_feature_name() -> None:
    ranked = rank_test_results(
        [
            {"feature": "zeta", "drift": 0.5},
            {"feature": "alpha", "drift": 0.5},
            {"feature": "beta", "drift": 0.8},
        ]
    )
    assert [item["feature"] for item in ranked] == ["beta", "alpha", "zeta"]
    assert [item["rank"] for item in ranked] == [1, 2, 3]


def test_quantile_distance_method_is_configurable(runtime) -> None:
    config, cache, _ = runtime
    quantile_config = config.updated(numeric_test=QUANTILE_DISTANCE)
    result = MonitorEngine(quantile_config, cache).run(3)
    amount = next(
        item for item in result.latest_results if item["feature"] == "transaction_amount"
    )
    assert amount["test_method"] == QUANTILE_DISTANCE
    assert amount["drift"] > amount["threshold"]
    assert amount["severity"] == SEVERITY_HIGH


def test_category_distance_is_total_variation(runtime) -> None:
    config, cache, _ = runtime
    result = MonitorEngine(config, cache).run(3)
    region = next(
        item for item in result.latest_results if item["feature"] == "region"
    )
    details = region["details"]
    expected = 0.5 * sum(details["absolute_differences"].values())
    assert region["drift"] == pytest.approx(expected, abs=1e-12)
    assert region["detected"] is True


def test_severity_boundaries() -> None:
    assert severity_for(0.79, 0.8) == SEVERITY_NONE
    assert severity_for(0.8, 0.8) == "LOW"
    assert severity_for(1.19, 0.8) == "LOW"
    assert severity_for(1.2, 0.8) == "MEDIUM"
    assert severity_for(2.0, 0.8) == "HIGH"

