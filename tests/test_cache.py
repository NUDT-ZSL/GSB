from __future__ import annotations

from dataclasses import replace

import pytest

from telemetry_drift.cache import (
    ContentAddressedCache,
    DependencyGraph,
    VariableRevision,
    build_window_cache_key,
)
from telemetry_drift.config import PipelineConfig
from telemetry_drift.errors import DriftError, E_CACHE_CORRUPT
from telemetry_drift.windows import build_window_specs
from telemetry_drift.alignment import align_observations, load_observations


def test_cache_key_covers_every_declared_input():
    base = dict(
        input_variable_content_hash="content",
        predictor_snapshot_version="v1:abc",
        residual_window_range=(1, 8),
        recycle_rate_threshold=0.2,
        retrain_config_digest="retrain",
    )
    key = build_window_cache_key(**base)
    assert key == build_window_cache_key(**base)
    for field, changed in (
        ("input_variable_content_hash", "other"),
        ("predictor_snapshot_version", "v2:abc"),
        ("residual_window_range", (2, 9)),
        ("recycle_rate_threshold", 0.3),
        ("retrain_config_digest", "other"),
    ):
        altered = dict(base)
        altered[field] = changed
        assert build_window_cache_key(**altered) != key


def test_cache_reuses_payload_and_detects_tampering(tmp_path):
    cache = ContentAddressedCache(tmp_path)
    key = "sha256:test"
    payload = {"value": 1, "windows": ["w0"]}
    cache.put(key, payload)
    assert cache.get(key) == payload
    path = cache._path(key)
    entry = path.read_text(encoding="utf-8").replace('"value":1', '"value":2')
    path.write_text(entry, encoding="utf-8")
    with pytest.raises(DriftError) as raised:
        cache.get(key)
    assert raised.value.code == E_CACHE_CORRUPT


def test_dependency_propagation_selects_predictor_closure_and_windows(data_dir):
    config = PipelineConfig.load(data_dir / "predictor_config.json")
    aligned = align_observations(
        load_observations(data_dir / "observations.jsonl"), config
    )
    specs = build_window_specs(aligned, config)
    graph = DependencyGraph.from_config(config, specs)
    plan = graph.propagate(
        changed_variables=[1],
        revisions=[VariableRevision(variable=1, start_ms=1900, end_ms=2100)],
    )
    assert plan.predictor_closure == ("predictor-main",)
    assert plan.affected_windows == ("w0002", "w0003", "w0004")
    predictor_plan = graph.propagate(changed_predictors=["predictor-main"])
    assert predictor_plan.affected_windows == tuple(
        spec.window_id for spec in specs
    )
    size_plan = graph.propagate(window_size_changed=True)
    assert size_plan.window_size_changed is True
    assert len(size_plan.affected_windows) == len(specs)

