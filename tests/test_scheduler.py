from __future__ import annotations

from netdiag.models import (
    DiagnosticConfig,
    ProbeObservation,
    ProbeSpec,
    RoundSpec,
    TargetSpec,
    Tolerance,
)
from netdiag.scheduler import execute_round


def _target(name: str, timeout: int = 100) -> TargetSpec:
    return TargetSpec(
        name=name,
        endpoint="http://127.0.0.1:1/x",
        expected_status=200,
        period_ms=200,
        timeout_ms=timeout,
        failure_threshold=3,
    )


def test_scheduler_distinguishes_timeout_active_and_not_started_abandonment() -> None:
    targets = {
        name: _target(name, timeout=200)
        for name in ["fast", "timeout", "cutoff", "fill", "tail"]
    }
    probes = [
        ProbeSpec(
            seq=index + 1,
            round_no=1,
            target=name,
            probe_type="http",
            logical_tick=0,
            plan_index=index,
        )
        for index, name in enumerate(targets)
    ]
    config = DiagnosticConfig(
        max_concurrency=2,
        round_budget_ms=100,
        window_size=4,
        window_step=2,
        loss_rate_threshold=0.3,
        latency_jitter_threshold_ms=10,
        tolerance=Tolerance(0.001, 0.5),
        clock_step_ms=1,
    )
    calls: list[int] = []

    def fake_probe(target: TargetSpec, probe: ProbeSpec) -> ProbeObservation:
        calls.append(probe.seq)
        if target.name == "fast":
            return ProbeObservation("success", 20, 200, "ok", 20)
        if target.name == "timeout":
            return ProbeObservation("timeout", 80, None, "timeout", 80)
        if target.name == "cutoff":
            return ProbeObservation("success", 50, 200, "ok", 50)
        return ProbeObservation("success", 20, 200, "ok", 20)

    execution = execute_round(
        round_spec=RoundSpec(round_no=1, logical_tick=0, probes=tuple(probes)),
        targets=targets,
        config=config,
        config_version=1,
        clock_watermark_ms=0,
        probe_fn=fake_probe,
    )
    by_target = {record.target: record for record in execution.records}
    assert execution.deadline_ms == 100
    assert by_target["fast"].outcome == "success"
    assert by_target["timeout"].outcome == "timeout"
    assert by_target["timeout"].counted_in_loss is True
    assert by_target["cutoff"].outcome == "abandoned"
    assert by_target["cutoff"].abandon_kind == "active_budget_cutoff"
    assert by_target["cutoff"].counted_in_loss is False
    assert by_target["tail"].outcome == "abandoned"
    assert by_target["tail"].abandon_kind == "not_started_budget"
    assert by_target["tail"].counted_in_loss is False
    assert by_target["fast"].logical_start_ms == 0
    assert by_target["timeout"].logical_start_ms == 0
    assert 5 not in calls
    assert execution.clock_after_ms == 100
    assert len(execution.abandoned) == 2


def test_scheduler_respects_concurrency_and_time_budget() -> None:
    targets = {
        f"t{index}": _target(f"t{index}")
        for index in range(7)
    }
    probes = tuple(
        ProbeSpec(
            seq=index + 1,
            round_no=1,
            target=f"t{index}",
            probe_type="http",
            logical_tick=0,
            plan_index=index,
        )
        for index in range(7)
    )
    config = DiagnosticConfig(
        max_concurrency=3,
        round_budget_ms=40,
        window_size=3,
        window_step=1,
        loss_rate_threshold=0.5,
        latency_jitter_threshold_ms=10,
        tolerance=Tolerance(0, 0),
        clock_step_ms=1,
    )
    active = 0
    peak = 0
    lock = __import__("threading").Lock()

    def fake_probe(_target: TargetSpec, _probe: ProbeSpec) -> ProbeObservation:
        nonlocal active, peak
        import time

        with lock:
            active += 1
            peak = max(peak, active)
        time.sleep(0.01)
        with lock:
            active -= 1
        return ProbeObservation("success", 20, 200, "ok", 20)

    execution = execute_round(
        round_spec=RoundSpec(round_no=1, logical_tick=0, probes=probes),
        targets=targets,
        config=config,
        config_version=1,
        clock_watermark_ms=0,
        probe_fn=fake_probe,
    )
    assert peak <= 3
    assert peak >= 2
    assert execution.deadline_ms == 40
    assert all(
        record.logical_start_ms < 40
        for record in execution.records
        if record.outcome != "abandoned"
    )
    assert any(
        record.abandon_kind == "not_started_budget"
        for record in execution.records
    )
