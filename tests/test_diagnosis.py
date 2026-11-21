from __future__ import annotations

from netdiag.diagnosis import diagnose_target
from netdiag.models import DiagnosticConfig, ProbeRecord, TargetSpec, Tolerance
from netdiag.scenario import canonical_json


def _config() -> DiagnosticConfig:
    return DiagnosticConfig(
        max_concurrency=2,
        round_budget_ms=100,
        window_size=6,
        window_step=2,
        loss_rate_threshold=0.3,
        latency_jitter_threshold_ms=15,
        tolerance=Tolerance(0.001, 0.5),
        clock_step_ms=1,
    )


def _target() -> TargetSpec:
    return TargetSpec(
        name="t",
        endpoint="http://127.0.0.1:1/x",
        expected_status=200,
        period_ms=100,
        timeout_ms=50,
        failure_threshold=3,
    )


def _record(
    seq: int,
    outcome: str,
    latency: int | None,
    abandon_kind: str | None = None,
) -> ProbeRecord:
    abandoned = outcome == "abandoned"
    return ProbeRecord(
        config_version=1,
        round_no=(seq - 1) // 4 + 1,
        target="t",
        seq=seq,
        probe_type="http",
        plan_index=seq - 1,
        logical_start_ms=seq * 10,
        logical_end_ms=seq * 10,
        outcome=outcome,
        abandon_kind=abandon_kind,
        counted_in_loss=outcome
        in {"status_mismatch", "timeout", "connection_refused", "malformed_response"},
        excluded_from_loss=abandoned,
        response_status=200 if outcome == "success" else None,
        expected_status=200,
        observed_latency_ms=latency,
        detail=f"fixture {outcome}",
    )


def test_all_failures_become_unreachable_and_repeat_is_identical() -> None:
    records = [
        _record(seq, "timeout", 50)
        for seq in range(1, 9)
    ]
    first = diagnose_target(
        target=_target(),
        records=records,
        config=_config(),
        config_version=1,
    )
    second = diagnose_target(
        target=_target(),
        records=records,
        config=_config(),
        config_version=1,
    )
    assert first.state == "unreachable"
    assert first.fault_type == "timeout"
    assert first.loss_rate == 1.0
    assert canonical_json(first.decision_chain) == canonical_json(second.decision_chain)
    assert first.decision_chain["first_trigger"]["rule"] in {
        "window_loss_rate",
        "consecutive_failures",
    }


def test_abandoned_only_is_budget_suspicion_not_packet_loss() -> None:
    records = [
        _record(seq, "abandoned", None, "not_started_budget")
        for seq in range(1, 9)
    ]
    diagnosis = diagnose_target(
        target=_target(),
        records=records,
        config=_config(),
        config_version=1,
    )
    assert diagnosis.state == "suspicious"
    assert diagnosis.fault_type == "budget_abandonment"
    assert diagnosis.loss_rate is None
    assert diagnosis.abandoned_count == 6
    assert len(diagnosis.decision_chain["excluded_abandoned"]) == 6
    assert all(
        item["excluded_from_denominator"]
        for item in diagnosis.decision_chain["excluded_abandoned"]
    )


def test_jitter_and_mixed_loss_transitions() -> None:
    jitter_records = [
        _record(seq, "success", 10 if seq % 2 else 60)
        for seq in range(1, 9)
    ]
    jitter = diagnose_target(
        target=_target(),
        records=jitter_records,
        config=_config(),
        config_version=1,
    )
    assert jitter.state == "suspicious"
    assert jitter.fault_type == "latency_jitter"

    mixed_records = [
        _record(
            seq,
            "success" if seq in {1, 3, 5, 7} else "connection_refused",
            None if seq not in {1, 3, 5, 7} else 20,
        )
        for seq in range(1, 9)
    ]
    mixed = diagnose_target(
        target=_target(),
        records=mixed_records,
        config=_config(),
        config_version=1,
    )
    assert mixed.state == "suspected_loss"
    assert mixed.loss_rate == 0.5
