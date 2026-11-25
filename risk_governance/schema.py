"""Schema-tolerant accessors for locally generated JSONL records."""

from __future__ import annotations

from typing import Any


def record_features(record: dict[str, Any]) -> dict[str, Any]:
    for key in ("features", "feature_columns", "feature_values", "feature_kv", "x"):
        value = record.get(key)
        if isinstance(value, dict):
            return value
    return {}


def record_score(record: dict[str, Any], field_name: str = "score") -> Any:
    if field_name != "score":
        value = record.get(field_name)
        if value is not None:
            return value
        features = record_features(record)
        return features.get(field_name)
    for key in ("score", "decision_score", "model_score", "prediction_score"):
        if record.get(key) is not None:
            return record[key]
    features = record_features(record)
    for key in ("score", "decision_score"):
        if key in features:
            return features[key]
    return None


def record_label(record: dict[str, Any]) -> Any:
    for key in ("label", "true_label", "y", "target"):
        if record.get(key) is not None:
            return record[key]
    return None
