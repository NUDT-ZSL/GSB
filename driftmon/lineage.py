"""Alert lineage replay from content-addressed statistics and test artifacts."""

from __future__ import annotations

from typing import Any

from .alerts import _summary
from .canonical import sha256_obj
from .cache import CacheStore
from .detectors import evaluate_feature_stats
from .errors import lineage_broken
from .models import CATEGORY, NUMERIC, FeatureSpec, MonitorConfig


def _feature_spec_from_stats(stats_content: dict[str, Any]) -> FeatureSpec:
    definition = stats_content["feature_definition"]
    return FeatureSpec(
        name=str(definition["name"]),
        kind=str(definition["kind"]),
        stat_version=int(stats_content["stat_version"]),
        enabled=bool(definition["enabled"]),
        mean=float(definition.get("mean", 0.0)),
        std=float(definition.get("std", 1.0)),
        levels=tuple(definition.get("levels", [])),
        probabilities=tuple(definition.get("probabilities", [])),
        description=str(definition.get("description", "")),
    )


def _config_for_replay(
    stats_content: dict[str, Any], test_content: dict[str, Any]
) -> MonitorConfig:
    feature = _feature_spec_from_stats(stats_content)
    if feature.kind == NUMERIC:
        quantiles = tuple(
            float(key)
            for key in stats_content["statistics"]["reference"]["quantiles"].keys()
        )
    else:
        quantiles = (0.5,)
    method = str(test_content.get("test_method", "mean_shift"))
    if method not in {"mean_shift", "quantile_distance"}:
        method = "mean_shift"
    threshold = test_content.get("threshold")
    numeric_threshold = float(threshold) if feature.kind == NUMERIC and threshold else 1.0
    category_threshold = (
        float(threshold) if feature.kind == CATEGORY and threshold else 0.2
    )
    return MonitorConfig(
        seed=0,
        n_batches=1,
        batch_size=1,
        window_size=1,
        slide_step=1,
        numeric_test=method,
        numeric_threshold=numeric_threshold,
        category_threshold=category_threshold,
        quantiles=quantiles,
        features=(feature,),
        drift_events=(),
    )


def replay_alert(alert: dict[str, Any], cache: CacheStore) -> dict[str, Any]:
    content = alert["content"]
    features = content.get("features")
    if not isinstance(features, list):
        raise lineage_broken(["alert.features"], "告警缺少特征明细")
    missing: list[str] = []
    replayed_records: list[dict[str, Any]] = []

    for record in features:
        feature_name = str(record.get("feature", "<unknown>"))
        stats_key = record.get("stats_key")
        test_key = record.get("test_key")
        if not stats_key:
            missing.append(f"stats_key:{feature_name}")
            continue
        if not test_key:
            missing.append(f"test_key:{feature_name}")
            continue
        stats_entry = cache.get(str(stats_key))
        if stats_entry is None:
            missing.append(f"cache:stats:{stats_key}")
            continue
        test_entry = cache.get(str(test_key))
        if test_entry is None:
            missing.append(f"cache:test:{test_key}")
            continue
        if stats_entry["content_sha256"] != record.get("stats_content_sha256"):
            missing.append(f"stats_checksum:{feature_name}:{stats_key}")
            continue
        if test_entry["content_sha256"] != record.get("test_content_sha256"):
            missing.append(f"test_checksum:{feature_name}:{test_key}")
            continue
        stats_content = stats_entry["content"]
        test_content = test_entry["content"]
        config = _config_for_replay(stats_content, test_content)
        feature = config.features[0]
        recomputed = evaluate_feature_stats(config, feature, stats_content)
        if recomputed != test_content:
            missing.append(f"consistent_test:{feature_name}:{test_key}")
            continue
        replayed_record = {
            "feature": recomputed["feature"],
            "kind": recomputed["kind"],
            "target_batch": recomputed["target_batch"],
            "drift": recomputed["drift"],
            "threshold": recomputed["threshold"],
            "detected": recomputed["detected"],
            "severity": recomputed["severity"],
            "test_method": recomputed["test_method"],
            "stats_key": stats_key,
            "test_key": test_key,
            "stats_content_sha256": stats_entry["content_sha256"],
            "test_content_sha256": test_entry["content_sha256"],
        }
        replayed_records.append(replayed_record)

    if missing:
        raise lineage_broken(missing)

    replayed_content = {
        "artifact_type": "drift_alert",
        "config_version": content["config_version"],
        "window": content["window"],
        "features": replayed_records,
        "summary": _summary(replayed_records),
    }
    replayed_checksum = sha256_obj(replayed_content)
    if replayed_checksum != alert["checksum"]:
        raise lineage_broken(
            ["alert_checksum"],
            "按追溯条目重放出的告警与旧校验和不一致",
        )
    return {
        "alert_id": alert["alert_id"],
        "version": alert["version"],
        "matches": True,
        "original_checksum": alert["checksum"],
        "replayed_checksum": replayed_checksum,
        "config_version": content["config_version"],
        "window": content["window"],
        "cache_keys": {
            "stats": [record["stats_key"] for record in replayed_records],
            "test": [record["test_key"] for record in replayed_records],
        },
    }

