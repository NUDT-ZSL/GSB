from __future__ import annotations

from dataclasses import replace

from telemetry_drift.alignment import align_observations, load_observations
from telemetry_drift.config import PipelineConfig


def test_alignment_is_byte_deterministic(data_dir):
    config = PipelineConfig.load(data_dir / "predictor_config.json")
    records = load_observations(data_dir / "observations.jsonl")
    first = align_observations(records, config)
    second = align_observations(records, config)
    assert first.serialized().encode("utf-8") == second.serialized().encode("utf-8")
    assert first.digest == second.digest
    assert len(first.times_ms) == 31


def test_forward_fill_and_minimal_change_have_distinct_flags(data_dir):
    base = PipelineConfig.load(data_dir / "predictor_config.json")
    records = load_observations(data_dir / "observations.jsonl")
    forward = align_observations(records, base)
    minimal_config = replace(
        base,
        alignment=replace(base.alignment, missing_strategy="minimal_change"),
    )
    minimal = align_observations(records, minimal_config)
    assert "forward_fill" in forward.fill_flags[0]
    assert "minimal_change" in minimal.fill_flags[0]
    assert forward.digest != minimal.digest


def test_missing_input_is_filled_without_losing_timeline(data_dir):
    config = PipelineConfig.load(data_dir / "predictor_config.json")
    aligned = align_observations(
        load_observations(data_dir / "observations.jsonl"), config
    )
    index = aligned.times_ms.index(900)
    assert aligned.fill_flags[0][index] == "forward_fill"
    assert aligned.values[0][index] == aligned.values[0][index - 1]


def test_zero_period_infers_common_sampling_grid(data_dir):
    config = PipelineConfig.load(data_dir / "predictor_config.json")
    inferred = replace(
        config,
        alignment=replace(config.alignment, period_ms=0),
    )
    aligned = align_observations(
        load_observations(data_dir / "observations.jsonl"), inferred
    )
    assert aligned.period_ms == 100
    assert len(aligned.times_ms) == 31

