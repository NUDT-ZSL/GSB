from __future__ import annotations

from driftmon.canonical import canonical_bytes
from driftmon.config import default_config
from driftmon.data import generate_batch, generate_stream
from driftmon.stats import build_feature_snapshot


def test_batch_and_fingerprint_are_byte_identical() -> None:
    config = default_config()
    left = generate_batch(config, 3)
    right = generate_batch(config, 3)
    assert left.fingerprint == right.fingerprint
    assert canonical_bytes(left.audit) == canonical_bytes(right.audit)
    assert canonical_bytes(
        [
            {
                "name": column.name,
                "kind": column.kind,
                "values": list(column.values),
                "sha": column.content_sha256,
            }
            for column in left.columns
        ]
    ) == canonical_bytes(
        [
            {
                "name": column.name,
                "kind": column.kind,
                "values": list(column.values),
                "sha": column.content_sha256,
            }
            for column in right.columns
        ]
    )


def test_same_batch_produces_identical_statistics_bytes() -> None:
    config = default_config()
    stream_left = generate_stream(config)
    stream_right = generate_stream(config)
    left_feature = config.feature("transaction_amount")
    right_feature = config.feature("transaction_amount")
    left = build_feature_snapshot(config, stream_left, left_feature, 4)
    right = build_feature_snapshot(config, stream_right, right_feature, 4)
    assert canonical_bytes(left) == canonical_bytes(right)


def test_numeric_and_category_scope_are_explicit() -> None:
    config = default_config()
    stream = generate_stream(config)
    numeric = build_feature_snapshot(
        config, stream, config.feature("transaction_amount"), 3
    )
    category = build_feature_snapshot(config, stream, config.feature("region"), 3)
    numeric_stats = numeric["statistics"]["monitor_window"]
    category_stats = category["statistics"]["monitor_window"]
    assert numeric_stats["kind"] == "numeric"
    assert "mean" in numeric_stats and "variance" in numeric_stats
    assert "quantiles" in numeric_stats
    assert "proportions" in category_stats
    assert "mean" not in category_stats
    assert "counts" in category_stats

