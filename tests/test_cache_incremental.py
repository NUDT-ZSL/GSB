from __future__ import annotations

from dataclasses import replace

from driftmon.cache import CacheStore
from driftmon.config import default_config
from driftmon.engine import MonitorEngine, compare_with_full_rebuild
from driftmon.models import CATEGORY


def test_advance_reuses_old_suffix_and_matches_full(runtime) -> None:
    config, cache, alerts = runtime
    engine = MonitorEngine(config, cache, alerts)
    first = engine.run(2)
    second = engine.run(3, previous_config=config, previous_cursor=2)
    assert len(first.recomputed_products) == 10
    assert len(first.reused_products) == 0
    assert len(second.recomputed_products) == 5
    assert len(second.reused_products) == 10
    assert set(map(tuple, second.recomputed_products)).isdisjoint(
        set(map(tuple, second.reused_products))
    )
    assert set(map(tuple, second.affected_products)).issubset(
        set(map(tuple, second.recomputed_products))
        | set(map(tuple, second.reused_products))
    )
    assert compare_with_full_rebuild(second, cache)["match"] is True


def test_window_change_recomputes_affected_batch_suffix(runtime) -> None:
    config, cache, alerts = runtime
    engine = MonitorEngine(config, cache, alerts)
    engine.run(4)
    changed = config.updated(window_size=2, slide_step=1)
    result = MonitorEngine(changed, cache, alerts).run(
        4, previous_config=config, previous_cursor=4
    )
    assert result.affected_batches == [2, 3, 4]
    assert result.affected_features == [
        "channel",
        "device_score",
        "region",
        "session_minutes",
        "transaction_amount",
    ]
    assert set(map(tuple, result.reused_products)) == {
        (feature.name, 1) for feature in changed.features
    }
    assert compare_with_full_rebuild(result, cache)["match"] is True


def test_stat_version_change_only_invalidates_one_feature(runtime) -> None:
    config, cache, alerts = runtime
    engine = MonitorEngine(config, cache, alerts)
    engine.run(4)
    features = tuple(
        feature.with_updates(stat_version=feature.stat_version + 1)
        if feature.name == "transaction_amount"
        else feature
        for feature in config.features
    )
    changed = config.updated(features=features)
    result = MonitorEngine(changed, cache, alerts).run(
        4, previous_config=config, previous_cursor=4
    )
    assert result.affected_features == ["transaction_amount"]
    assert result.affected_batches == [1, 2, 3, 4]
    assert len(result.recomputed_products) == 4
    assert len(result.reused_products) == 16
    assert compare_with_full_rebuild(result, cache)["match"] is True


def test_disable_and_enable_feature_matches_full_rebuild(runtime) -> None:
    config, cache, alerts = runtime
    engine = MonitorEngine(config, cache, alerts)
    engine.run(4)
    disabled_features = tuple(
        feature.with_updates(enabled=False) if feature.name == "region" else feature
        for feature in config.features
    )
    disabled_config = config.updated(features=disabled_features)
    disabled = MonitorEngine(disabled_config, cache, alerts).run(
        4, previous_config=config, previous_cursor=4
    )
    region = next(
        item for item in disabled.latest_results if item["feature"] == "region"
    )
    assert region["status"] == "disabled"
    assert region["severity"] == "DISABLED"
    assert compare_with_full_rebuild(disabled, cache)["match"] is True

    enabled_features = tuple(
        feature.with_updates(enabled=True) if feature.name == "region" else feature
        for feature in disabled_config.features
    )
    enabled_config = disabled_config.updated(features=enabled_features)
    enabled = MonitorEngine(enabled_config, cache, alerts).run(
        4, previous_config=disabled_config, previous_cursor=4
    )
    assert set(item[0] for item in enabled.affected_products) == {"region"}
    assert set(item[0] for item in enabled.reused_products) == {
        "channel",
        "device_score",
        "region",
        "session_minutes",
        "transaction_amount",
    }
    assert compare_with_full_rebuild(enabled, cache)["match"] is True


def test_column_redefinition_only_invalidates_redefined_column(runtime) -> None:
    config, cache, alerts = runtime
    engine = MonitorEngine(config, cache, alerts)
    engine.run(4)
    features = []
    for feature in config.features:
        if feature.name == "device_score":
            features.append(
                feature.with_updates(
                    kind=CATEGORY,
                    levels=("high", "low", "mid"),
                    probabilities=(0.25, 0.5, 0.25),
                    mean=0.0,
                    std=1.0,
                )
            )
        else:
            features.append(feature)
    events = tuple(
        event
        for event in config.drift_events
        if not (
            event.feature == "device_score"
            and event.kind in {"mean_shift", "variance_scale"}
        )
    )
    changed = config.updated(features=tuple(features), drift_events=events)
    result = MonitorEngine(changed, cache, alerts).run(
        4, previous_config=config, previous_cursor=4
    )
    assert result.affected_features == ["device_score"]
    assert len(result.recomputed_products) == 4
    device = next(
        item for item in result.latest_results if item["feature"] == "device_score"
    )
    assert device["kind"] == CATEGORY
    assert compare_with_full_rebuild(result, cache)["match"] is True


def test_cache_key_components_include_required_lineage(runtime) -> None:
    config, cache, alerts = runtime
    result = MonitorEngine(config, cache, alerts).run(3)
    product = result.products[0]
    stats_explained = cache.explain(product.stats_key)
    test_explained = cache.explain(product.test_key)
    stats_components = stats_explained["key_components"]
    test_components = test_explained["key_components"]
    for component in (
        "reference_window_sha256",
        "test_method",
        "numeric_threshold",
        "stat_version",
        "feature_definition_sha256",
        "upstream_product_hashes",
    ):
        assert component in stats_components
    for component in (
        "reference_window_sha256",
        "test_method",
        "threshold",
        "stat_version",
        "feature_definition_sha256",
        "upstream_product_hashes",
    ):
        assert component in test_components
    assert test_components["upstream_product_hashes"]["stats"] == product.stats_content_sha256

