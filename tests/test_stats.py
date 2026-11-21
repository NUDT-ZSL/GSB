from __future__ import annotations

from netdiag.models import DiagnosticConfig, ProbeRecord, Tolerance
from netdiag.stats import compute_windows, percentile


def _config(window_size: int = 5, window_step: int = 2) -> DiagnosticConfig:
    return DiagnosticConfig(
        max_concurrency=2,
        round_budget_ms=100,
        window_size=window_size,
        window_step=window_step,
        loss_rate_threshold=0.3,
        latency_jitter_threshold_ms=10,
        tolerance=Tolerance(0.001, 0.5),
        clock_step_ms=1,
    )


def _record(
    seq: int,
    outcome: str,
    *,
    latency: int | None = None,
    abandon_kind: str | None = None,
) -> ProbeRecord:
    abandoned = outcome == "abandoned"
    return ProbeRecord(
        config_version=1,
        round_no=(seq - 1) // 5 + 1,
        target="t",
        seq=seq,
        probe_type="http",
        plan_index=seq - 1,
        logical_start_ms=seq * 10,
        logical_end_ms=seq * 10 + (latency or 0),
        outcome=outcome,
        abandon_kind=abandon_kind,
        counted_in_loss=outcome
        in {"status_mismatch", "timeout", "connection_refused", "malformed_response"},
        excluded_from_loss=abandoned,
        response_status=200 if outcome in {"success", "status_mismatch"} else None,
        expected_status=200,
        observed_latency_ms=latency,
        detail="fixture",
    )


def test_percentile_uses_sorted_linear_interpolation() -> None:
    assert percentile([50, 10, 20, 30, 40], 0.50) == 30.0
    assert percentile([10, 20, 30, 40, 50], 0.95) == 48.0
    assert percentile([10], 0.95) == 10.0
    assert percentile([], 0.95) is None


def test_window_excludes_abandon_from_denominator_and_lists_them() -> None:
    records = [
        _record(1, "success", latency=10),
        _record(2, "abandoned", abandon_kind="not_started_budget"),
        _record(3, "timeout", latency=40),
        _record(4, "success", latency=30),
        _record(5, "abandoned", abandon_kind="active_budget_cutoff"),
        _record(6, "success", latency=20),
    ]
    windows = compute_windows(records, _config(5, 2), 1, "t")
    assert len(windows) == 2
    first = windows[0]
    assert first.sample_count == 5
    assert first.observed_count == 3
    assert first.success_count == 2
    assert first.failure_count == 1
    assert first.abandoned_count == 2
    assert first.loss_rate == 0.333333
    assert first.p50_ms == 20.0
    assert [item["seq"] for item in first.excluded_abandoned] == [2, 5]
    assert all(item["excluded_from_denominator"] for item in first.excluded_abandoned)
    second = windows[1]
    assert second.start_position == 2 and second.end_position == 5
    assert second.observed_count == 3
    assert second.abandoned_count == 1
    assert second.loss_rate == 0.333333


def test_windows_are_deterministic_for_same_records() -> None:
    records = [_record(seq, "success", latency=seq * 3) for seq in range(1, 12)]
    a = [window.as_dict() for window in compute_windows(records, _config(5, 2), 1, "t")]
    b = [window.as_dict() for window in compute_windows(records, _config(5, 2), 1, "t")]
    assert a == b
    assert a[0]["start_position"] == 0 and a[0]["end_position"] == 4
    assert a[-1]["end_position"] == 10
