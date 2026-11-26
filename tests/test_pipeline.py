from __future__ import annotations

from telemetry_drift.canonical import canonical_json
from telemetry_drift.config import PipelineConfig
from telemetry_drift.ledger import replay_ledger
from telemetry_drift.pipeline import compare_snapshot_runs, run_pipeline


def test_pipeline_is_byte_deterministic(data_dir):
    config = PipelineConfig.load(data_dir / "predictor_config.json")
    first = run_pipeline(
        config,
        data_dir / "observations.jsonl",
        data_dir / "predictor_v1.json",
    )
    second = run_pipeline(
        config,
        data_dir / "observations.jsonl",
        data_dir / "predictor_v1.json",
    )
    assert canonical_json(first) == canonical_json(second)
    assert first["comparison_digest"] == second["comparison_digest"]


def test_v1_v2_retraining_and_error_replay_are_consistent(data_dir):
    config = PipelineConfig.load(data_dir / "predictor_config.json")
    comparison = compare_snapshot_runs(
        config,
        data_dir / "observations.jsonl",
        data_dir / "predictor_v1.json",
        data_dir / "predictor_v2.json",
    )
    assert comparison["ok"] is True
    result = run_pipeline(
        config,
        data_dir / "observations.jsonl",
        data_dir / "predictor_v1.json",
    )
    assert replay_ledger(result["ledger_entries"], config)["ok"] is True
    assert [closure["covered_windows"] for closure in result["closures"]] == [
        ["w0001", "w0002", "w0003", "w0004"]
    ]
    assert result["retrain"]["reused_windows"] == ["w0000"]


def test_content_cache_and_targeted_invalidation(data_dir, tmp_path):
    config = PipelineConfig.load(data_dir / "predictor_config.json")
    cache = tmp_path / "cache"
    first = run_pipeline(
        config,
        data_dir / "observations.jsonl",
        data_dir / "predictor_v1.json",
        cache_dir=cache,
    )
    second = run_pipeline(
        config,
        data_dir / "observations.jsonl",
        data_dir / "predictor_v1.json",
        cache_dir=cache,
    )
    assert first["cache"]["run_hit"] is False
    assert second["cache"]["run_hit"] is True
    assert second["cache"]["recomputed_windows"] == []
    changed = run_pipeline(
        config,
        data_dir / "observations.jsonl",
        data_dir / "predictor_v1.json",
        cache_dir=cache,
        changes={
            "changed_variables": [1],
            "revisions": [{"variable": 1, "start_ms": 1800, "end_ms": 2200}],
        },
    )
    assert changed["cache"]["recomputed_windows"] == [
        "w0001",
        "w0002",
        "w0003",
        "w0004",
    ]
    assert changed["cache"]["reused_windows"] == ["w0000"]

