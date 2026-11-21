from __future__ import annotations

from capbench.scenario import GradientSpec
from capbench.workload import PlannedRequest, RequestOutcome, summarize_window


def _requests(count: int) -> list[PlannedRequest]:
    return [
        PlannedRequest(
            sequence=100 + index,
            worker=index % 2,
            planned_ms=index * 10,
            started_ms=index * 10,
        )
        for index in range(count)
    ]


def test_error_categories_are_isolated_and_diagnosable() -> None:
    gradient = GradientSpec(
        index=2,
        concurrency=2,
        duration_ms=1000,
        pace_ms=100,
        window_ms=1000,
        request_timeout_ms=100,
        logical_start_ms=1000,
    )
    outcomes = [
        RequestOutcome(status=200, latency_ms=10),
        RequestOutcome(status=500, latency_ms=12, category="status_mismatch", detail="expected 200, received 500"),
        RequestOutcome(category="connection_refused", detail="refused"),
        RequestOutcome(status=200, latency_ms=150, category="request_timeout", detail="slow"),
        RequestOutcome(status=200, latency_ms=11, category="malformed_response", detail="bad body"),
        RequestOutcome(status=503, latency_ms=20, category="target_service_interrupted", detail="fault"),
        RequestOutcome(status=200, latency_ms=20),
    ]
    summary = summarize_window(
        tenant="alpha",
        gradient=gradient,
        window_index=0,
        requests=_requests(7),
        outcomes=outcomes,
        expected_status=200,
    )
    assert summary["request_count"] == 7
    assert summary["success_count"] == 2
    assert summary["error_counts"] == {
        "status_mismatch": 1,
        "connection_refused": 1,
        "request_timeout": 1,
        "malformed_response": 1,
        "target_service_interrupted": 1,
    }
    assert summary["request_seq_start"] == 100
    assert summary["request_seq_end"] == 106
    assert summary["p95_latency_ms"] == 117.5
    by_category = {item["category"]: item for item in summary["anomalies"]}
    assert by_category["connection_refused"]["request_seq_ranges"] == [[102, 102]]
    assert by_category["malformed_response"]["tenant"] == "alpha"
    assert by_category["malformed_response"]["gradient_index"] == 2
    assert by_category["malformed_response"]["window_index"] == 0


def test_one_bad_window_does_not_pollute_another() -> None:
    gradient = GradientSpec(
        index=1,
        concurrency=1,
        duration_ms=2000,
        pace_ms=100,
        window_ms=1000,
        request_timeout_ms=100,
        logical_start_ms=0,
    )
    bad = summarize_window(
        tenant="alpha",
        gradient=gradient,
        window_index=0,
        requests=_requests(2),
        outcomes=[
            RequestOutcome(status=500, latency_ms=10, category="status_mismatch"),
            RequestOutcome(status=500, latency_ms=10, category="status_mismatch"),
        ],
        expected_status=200,
    )
    good = summarize_window(
        tenant="alpha",
        gradient=gradient,
        window_index=1,
        requests=[
            PlannedRequest(sequence=200, worker=0, planned_ms=1000, started_ms=1000),
        ],
        outcomes=[RequestOutcome(status=200, latency_ms=10)],
        expected_status=200,
    )
    assert bad["error_counts"]["status_mismatch"] == 2
    assert good["error_counts"]["status_mismatch"] == 0
    assert good["success_count"] == 1
    assert good["anomalies"] == []

