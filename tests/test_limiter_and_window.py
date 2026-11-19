from __future__ import annotations

from gateway.service import GatewayService

from .conftest import SAMPLES, call_gateway, principals, sample_config, update_raw


def _install_high_limit_config(service, *, max_requests=100, max_rejections=100):
    raw = sample_config(2)
    raw["limits"]["sliding_window"]["max_requests"] = max_requests
    raw["limits"]["sliding_window"]["max_rejections"] = max_rejections
    for bucket in raw["limits"]["route_buckets"].values():
        bucket["capacity"] = 100
    for bucket in raw["limits"]["tenant_buckets"].values():
        bucket["capacity"] = 100
    update_raw(service, raw, expected=1)


def test_window_only_uses_completed_buckets_and_unique_assignment(service):
    _install_high_limit_config(service, max_requests=2, max_rejections=100)
    p = principals()
    first = call_gateway(
        service,
        principal=p["api_a_v2"],
        request_id="win-1",
        request_seq=1,
        logical_time_ms=10_000,
    )
    second = call_gateway(
        service,
        principal=p["api_a_v2"],
        request_id="win-2",
        request_seq=2,
        logical_time_ms=10_010,
    )
    still_current_bucket = call_gateway(
        service,
        principal=p["api_a_v2"],
        request_id="win-3",
        request_seq=3,
        logical_time_ms=10_020,
    )
    assert [first.status, second.status, still_current_bucket.status] == [200, 200, 200]

    boundary = call_gateway(
        service,
        principal=p["api_a_v2"],
        request_id="win-4",
        request_seq=4,
        logical_time_ms=10_250,
    )
    assert boundary.status == 429
    assert boundary.code == "E_WINDOW_REQUEST_LIMIT"
    limit_stage = next(
        item
        for item in boundary.decision_chain
        if item["stage"] == "limit" and "window" in item
    )
    assert limit_stage["window"]["completed_attempts"] == 3
    assert limit_stage["window"]["bucket_index"] == 41

    state = service.state_snapshot("tenant-a")
    buckets = {
        (item["route_id"], item["bucket_index"]): item
        for item in state["window_buckets"]
    }
    assert buckets[("orders", 40)]["attempts"] == 3
    assert buckets[("orders", 40)]["rejections"] == 0
    assert buckets[("orders", 41)]["attempts"] == 1
    assert buckets[("orders", 41)]["rejections"] == 1


def test_window_state_survives_restart_without_reassignment(
    service, database_path, upstream_server
):
    _install_high_limit_config(service, max_requests=2, max_rejections=100)
    p = principals()
    for index in range(3):
        response = call_gateway(
            service,
            principal=p["api_a_v2"],
            request_id=f"restart-a-{index}",
            request_seq=index + 1,
            logical_time_ms=10_000,
        )
        assert response.status == 200
    boundary = call_gateway(
        service,
        principal=p["api_a_v2"],
        request_id="restart-boundary",
        request_seq=4,
        logical_time_ms=10_250,
    )
    assert boundary.code == "E_WINDOW_REQUEST_LIMIT"
    before = service.state_snapshot("tenant-a")["window_buckets"]

    restarted = GatewayService(
        database_path,
        initial_config_path=SAMPLES / "config_v1.json",
    )
    after = restarted.state_snapshot("tenant-a")["window_buckets"]
    assert after == before
    replay = call_gateway(
        restarted,
        principal=p["api_a_v2"],
        request_id="restart-after",
        request_seq=5,
        logical_time_ms=10_251,
    )
    assert replay.code == "E_WINDOW_REQUEST_LIMIT"
    final_state = restarted.state_snapshot("tenant-a")["window_buckets"]
    initial_bucket = next(
        item for item in final_state if item["bucket_index"] == 40
    )
    assert initial_bucket["attempts"] == 3
    boundary_bucket = next(
        item for item in final_state if item["bucket_index"] == 41
    )
    assert boundary_bucket["attempts"] == 2
    assert boundary_bucket["rejections"] == 2


def test_rejection_count_feeds_only_next_completed_window(service):
    raw = sample_config(2)
    raw["limits"]["sliding_window"]["max_requests"] = 100
    raw["limits"]["sliding_window"]["max_rejections"] = 1
    raw["limits"]["route_buckets"]["orders"] = {
        "capacity": 1,
        "refill_per_second": 1,
    }
    raw["limits"]["tenant_buckets"]["tenant-a"] = {
        "capacity": 100,
        "refill_per_second": 1,
    }
    update_raw(service, raw, expected=1)
    p = principals()
    admitted = call_gateway(
        service,
        principal=p["api_a_v2"],
        request_id="rej-1",
        request_seq=1,
        logical_time_ms=10_000,
    )
    denied = call_gateway(
        service,
        principal=p["api_a_v2"],
        request_id="rej-2",
        request_seq=2,
        logical_time_ms=10_001,
    )
    assert admitted.status == 200
    assert denied.code == "E_RATE_LIMITED"
    same_bucket = call_gateway(
        service,
        principal=p["api_a_v2"],
        request_id="rej-3",
        request_seq=3,
        logical_time_ms=10_100,
    )
    assert same_bucket.code == "E_RATE_LIMITED"
    next_window = call_gateway(
        service,
        principal=p["api_a_v2"],
        request_id="rej-4",
        request_seq=4,
        logical_time_ms=10_250,
    )
    assert next_window.code == "E_WINDOW_REJECTION_LIMIT"


def test_token_refill_uses_only_injected_millisecond_clock(service):
    raw = sample_config(2)
    raw["limits"]["route_buckets"]["orders"] = {
        "capacity": 100,
        "refill_per_second": 1,
    }
    raw["limits"]["tenant_buckets"]["tenant-a"] = {
        "capacity": 1,
        "refill_per_second": 1,
    }
    raw["limits"]["sliding_window"]["max_requests"] = 100
    update_raw(service, raw, expected=1)
    p = principals()
    first = call_gateway(
        service,
        principal=p["api_a_v2"],
        request_id="refill-1",
        request_seq=1,
        logical_time_ms=10_000,
    )
    too_early = call_gateway(
        service,
        principal=p["api_a_v2"],
        request_id="refill-2",
        request_seq=2,
        logical_time_ms=10_999,
    )
    exact = call_gateway(
        service,
        principal=p["api_a_v2"],
        request_id="refill-3",
        request_seq=3,
        logical_time_ms=11_000,
    )
    assert first.status == 200
    assert too_early.code == "E_RATE_LIMITED"
    assert exact.status == 200
    state = service.state_snapshot("tenant-a")
    tenant_bucket = next(
        item
        for item in state["token_buckets"]
        if item["bucket_kind"] == "tenant" and item["bucket_key"] == "tenant-a"
    )
    assert tenant_bucket["tokens_units"] == 0
    assert tenant_bucket["last_refill_ms"] == 11_000


def test_clock_rewind_beyond_tolerance_is_atomic_and_keeps_balances(service):
    p = principals()
    first = call_gateway(
        service,
        principal=p["api_a_v2"],
        request_id="clock-1",
        request_seq=1,
        logical_time_ms=20_000,
    )
    assert first.status == 200
    before = service.state_snapshot("tenant-a")
    rewind = call_gateway(
        service,
        principal=p["api_a_v2"],
        request_id="clock-2",
        request_seq=2,
        logical_time_ms=19_000,
    )
    assert rewind.status == 409
    assert rewind.code == "E_CLOCK_REWIND"
    after = service.state_snapshot("tenant-a")
    assert after["token_buckets"] == before["token_buckets"]
    assert after["window_buckets"] == before["window_buckets"]
    assert after["logical_clock_ms"] == before["logical_clock_ms"]

    within_tolerance = call_gateway(
        service,
        principal=p["api_a_v2"],
        request_id="clock-3",
        request_seq=2,
        logical_time_ms=19_975,
    )
    assert within_tolerance.status == 200
    clock_stage = next(
        item for item in within_tolerance.decision_chain if item["stage"] == "clock"
    )
    assert clock_stage["effective_ms"] == 20_000
    assert clock_stage["within_tolerance_adjustment_ms"] == 25


def test_sequence_is_bit_for_bit_reproducible(database_path, upstream_server, tmp_path):
    p = principals()
    sequence = [
        (9_000, "api_a_v1", 200),
        (10_000, "api_a_v2", 200),
        (15_000, "api_a_v1", 401),
        (20_000, "api_a_v2", 200),
    ]
    outcomes = []
    for run in range(2):
        service = GatewayService(
            tmp_path / f"deterministic-{run}.sqlite",
            initial_config_path=SAMPLES / "config_v1.json",
        )
        run_outcomes = []
        for index, (logical_time, principal_name, expected_status) in enumerate(sequence):
            response = call_gateway(
                service,
                principal=p[principal_name],
                request_id=f"det-{run}-{index}",
                request_seq=index + 1,
                logical_time_ms=logical_time,
                path="/v1/down",
            )
            run_outcomes.append((response.status, response.code, expected_status))
        outcomes.append(run_outcomes)
    assert outcomes[0] == outcomes[1]
    assert [item[0] for item in outcomes[0]] == [502, 502, 401, 502]
