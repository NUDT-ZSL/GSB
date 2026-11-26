from __future__ import annotations

from dataclasses import dataclass

from telemetry_drift.alignment import align_observations, load_observations
from telemetry_drift.config import PipelineConfig
from telemetry_drift.retraining import (
    build_closures,
    retrain_closures,
    verify_non_closure_reuse,
)
from telemetry_drift.snapshots import read_snapshot
from telemetry_drift.statistics import WindowStatistics
from telemetry_drift.thresholds import decide_joint
from telemetry_drift.windows import WindowEvidence, build_window_evidence


def test_closure_retrains_only_covered_samples(data_dir):
    config = PipelineConfig.load(data_dir / "predictor_config.json")
    aligned = align_observations(
        load_observations(data_dir / "observations.jsonl"), config
    )
    model = read_snapshot(data_dir / "predictor_v1.json").current
    windows = build_window_evidence(aligned, model, config)
    outcome = retrain_closures(windows, model, config)
    assert [closure.covered_windows for closure in outcome.closures] == [
        ("w0001", "w0002", "w0003", "w0004")
    ]
    assert outcome.reused_windows == ("w0000",)
    assert verify_non_closure_reuse(windows, outcome)
    by_id = {window.window_id: window for window in windows}
    closure_samples = set(outcome.closures[0].sample_indices)
    covered_window_samples = {
        index
        for window_id in outcome.closures[0].covered_windows
        for index in by_id[window_id].sample_indices
    }
    assert closure_samples == covered_window_samples
    for retrain in outcome.retrains:
        assert retrain.model_after.retrain_generation == 1
        assert retrain.model_after != retrain.model_before
        assert len(retrain.retrain_samples) == len(closure_samples)
    assert WindowEvidence.from_dict(windows[0].to_dict()) == windows[0]


def _fake_window(
    ordinal: int,
    score: float,
    *,
    start_ms: int,
    drift: bool = True,
    config: PipelineConfig,
) -> WindowEvidence:
    stats = WindowStatistics(
        mean=score,
        variance=0.01,
        standard_deviation=0.1,
        skewness=0.0,
        trend=0.0,
        upstream_contribution=0.0,
        input_amplitude=score,
        drift_score=score,
        effective_sample_count=8.0,
    )
    decision = decide_joint(
        stats.input_amplitude,
        stats.drift_score,
        input_threshold=config.predictor.input_amplitude_threshold,
        predictor_threshold=config.predictor.drift_threshold,
        config=config.thresholds,
    )
    if not drift:
        decision = decide_joint(
            0.0,
            0.0,
            input_threshold=config.predictor.input_amplitude_threshold,
            predictor_threshold=config.predictor.drift_threshold,
            config=config.thresholds,
        )
    return WindowEvidence(
        window_id=f"w{ordinal:04d}",
        ordinal=ordinal,
        start_index=ordinal * 2,
        end_index=ordinal * 2 + 7,
        start_ms=start_ms,
        end_ms=start_ms + 700,
        backtrack_start_ms=max(0, start_ms - 1000),
        sample_indices=tuple(range(ordinal * 2, ordinal * 2 + 8)),
        raw_residuals=(0.0,) * 8,
        feature_deltas=((0.0,),) * 8,
        features=((0.0,),) * 8,
        targets=(0.0,) * 8,
        predictions=(0.0,) * 8,
        coefficients=(0.0,),
        statistics=stats,
        decision=decision,
        digest=f"fake-{ordinal}",
    )


def test_equal_scores_merge_in_start_time_order(data_dir):
    config = PipelineConfig.load(data_dir / "predictor_config.json")
    windows = [
        _fake_window(0, 1.5, start_ms=0, drift=False, config=config),
        _fake_window(1, 2.0, start_ms=200, config=config),
        _fake_window(2, 2.0, start_ms=400, config=config),
        _fake_window(3, 1.5, start_ms=600, drift=False, config=config),
        _fake_window(4, 1.5, start_ms=800, drift=False, config=config),
    ]
    closures = build_closures(windows, closure_radius=1)
    assert len(closures) == 1
    assert closures[0].covered_windows == ("w0000", "w0001", "w0002", "w0003")
    assert closures[0].event_windows == ("w0001", "w0002")

