from __future__ import annotations

import dataclasses
import socket
import threading
from types import SimpleNamespace

from capbench.clock import ClockSegment, ClockSpec
from capbench.probe import HttpProbe
from capbench.scenario import GradientSpec, Service, SLO, Tenant
from capbench.test_service import ProgrammableHTTPServer
from capbench.workload import build_window_requests


def _gradient() -> GradientSpec:
    return GradientSpec(
        index=1,
        concurrency=4,
        duration_ms=5000,
        pace_ms=100,
        window_ms=1000,
        request_timeout_ms=100,
        logical_start_ms=0,
    )


def test_actual_http_error_classification() -> None:
    tenant = Tenant(
        name="alpha",
        slo=SLO(0.99, 50),
        profile={
            "status_distribution": {"200": 1.0},
            "fault_windows": [1],
            "malformed_windows": [2],
            "refused_windows": [3],
            "slow_windows": [4],
            "latency": {
                "base_ms": 10,
                "jitter_ms": 0,
                "per_concurrency_ms": 0,
                "slow_extra_ms": 500,
            },
        },
    )
    service = Service(
        name="http-test",
        host="127.0.0.1",
        port=1,
        expected_status=200,
        tenants=(tenant,),
    )
    fake_scenario = SimpleNamespace(
        seed=123,
        services=(service,),
    )
    server = ProgrammableHTTPServer(("127.0.0.1", 0), fake_scenario, service.name)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    actual_service = dataclasses.replace(service, port=server.server_address[1])
    clock = ClockSpec((ClockSegment(0, 5000, 10),))
    gradient = _gradient()
    probe = HttpProbe()
    try:
        outcomes_by_window = {}
        for window_index in range(5):
            requests = build_window_requests(
                gradient,
                window_index,
                clock,
                1,
            )
            outcomes = probe.probe_window(
                service=actual_service,
                tenant="alpha",
                gradient=gradient,
                window_index=window_index,
                requests=requests,
                expected_status=200,
                timeout_ms=100,
                seed=123,
                run_id="test",
            )
            outcomes_by_window[window_index] = outcomes
        assert all(item.category is None for item in outcomes_by_window[0])
        assert all(
            item.category == "target_service_interrupted"
            for item in outcomes_by_window[1]
        )
        assert all(
            item.category == "malformed_response"
            for item in outcomes_by_window[2]
        )
        assert all(
            item.category == "connection_refused"
            for item in outcomes_by_window[3]
        )
        assert all(
            item.category == "request_timeout"
            for item in outcomes_by_window[4]
        )
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=3)


def test_actual_tcp_refusal_is_classified() -> None:
    tenant = Tenant(
        name="alpha",
        slo=SLO(0.99, 50),
        profile={"status_distribution": {"200": 1.0}},
    )
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        closed_port = sock.getsockname()[1]
    service = Service(
        name="closed",
        host="127.0.0.1",
        port=closed_port,
        expected_status=200,
        tenants=(tenant,),
    )
    gradient = _gradient()
    clock = ClockSpec((ClockSegment(0, 1000, 10),))
    requests = build_window_requests(gradient, 0, clock, 1)
    outcomes = HttpProbe().probe_window(
        service=service,
        tenant="alpha",
        gradient=gradient,
        window_index=0,
        requests=requests[:2],
        expected_status=200,
        timeout_ms=100,
        seed=1,
        run_id="closed",
    )
    assert [item.category for item in outcomes] == [
        "connection_refused",
        "connection_refused",
    ]

