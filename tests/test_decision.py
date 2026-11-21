from __future__ import annotations

from capbench.decision import judge_gradient, judge_window
from capbench.scenario import SLO


def _summary(
    *,
    window_index: int,
    success_rate: float,
    p95: float,
    interrupted: int = 0,
) -> dict:
    return {
        "tenant": "alpha",
        "gradient_index": 1,
        "window_index": window_index,
        "success_rate": success_rate,
        "p95_latency_ms": p95,
        "error_counts": {"target_service_interrupted": interrupted},
        "request_seq_start": 1,
        "request_seq_end": 10,
    }


def test_window_verdicts_pass_degraded_abort() -> None:
    slo = SLO(0.99, 50, degraded_success_margin=0.01, degraded_latency_ratio=1.2)
    assert judge_window(_summary(window_index=0, success_rate=1.0, p95=50), slo)["verdict"] == "pass"
    assert judge_window(_summary(window_index=1, success_rate=0.99, p95=55), slo)["verdict"] == "degraded_pass"
    assert judge_window(_summary(window_index=2, success_rate=0.95, p95=20), slo)["verdict"] == "abort"
    assert judge_window(_summary(window_index=3, success_rate=1.0, p95=20, interrupted=1), slo)["verdict"] == "abort"


def test_early_termination_trigger_evidence() -> None:
    slo = SLO(0.99, 50, degraded_success_margin=0.01, degraded_latency_ratio=1.2)
    decision = judge_gradient(
        summaries=[
            _summary(window_index=0, success_rate=1.0, p95=20),
            _summary(window_index=1, success_rate=0.9, p95=20),
        ],
        slo=slo,
        total_windows=3,
        allow_early_termination=True,
    )
    assert decision["verdict"] == "abort"
    assert decision["terminated_early"] is True
    assert decision["trigger"]["window_index"] == 1
    assert "success_rate_below_hard_floor" in decision["trigger"]["reasons"]

