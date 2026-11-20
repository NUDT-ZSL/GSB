"""Requirement 8 (error chain): upstream failures map to distinct E_xxx
codes over the local loopback test service, and one service's anomaly
does not pollute other services in the same round."""
import socket
import subprocess
import sys
import time

import pytest

from gateway import errors
from gateway.clock import LogicalClock
from gateway.engine import Engine
from gateway.errors import GatewayError
from gateway.store import Store
from gateway.upstream import HttpUpstream, SimulatedUpstream


def _free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture()
def loopback_server():
    port = _free_port()
    proc = subprocess.Popen(
        [sys.executable, "-m", "gateway.upstream_server",
         "--port", str(port)],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    try:
        for _ in range(50):
            try:
                with socket.create_connection(("127.0.0.1", port), 0.2):
                    break
            except OSError:
                time.sleep(0.1)
        yield port
    finally:
        proc.terminate()
        proc.wait()


def test_conn_refused_maps_to_error_code():
    with socket.socket() as s:  # closed port -> connection refused
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    upstream = HttpUpstream(port)
    with pytest.raises(GatewayError) as exc:
        upstream.call("svc", "r1", 10, 5, 300)
    assert exc.value.code == errors.E_UPSTREAM_CONN_REFUSED
    assert exc.value.fields["request_id"] == "r1"


def test_budget_exceeded_maps_to_error_code(loopback_server):
    upstream = HttpUpstream(loopback_server)
    with pytest.raises(GatewayError) as exc:
        upstream.call("svc", "r2", 10, 5, 300, fault="budget_exceeded")
    assert exc.value.code == errors.E_UPSTREAM_BUDGET
    assert exc.value.fields["budget_ms"] == 300


def test_digest_mismatch_maps_to_error_code(loopback_server):
    upstream = HttpUpstream(loopback_server)
    with pytest.raises(GatewayError) as exc:
        upstream.call("svc", "r3", 10, 5, 300, fault="digest_mismatch")
    assert exc.value.code == errors.E_UPSTREAM_DIGEST
    assert "expected" in exc.value.fields and "actual" in exc.value.fields


def test_successful_call_returns_digest(loopback_server):
    upstream = HttpUpstream(loopback_server)
    result = upstream.call("svc", "r4", 10, 5, 300)
    assert len(result["digest"]) == 64


def test_clock_rollback_beyond_threshold():
    clock = LogicalClock(rollback_threshold_ms=5)
    clock.advance_to(1000)
    clock.advance_to(996)  # small rollback tolerated and clamped
    assert clock.now_ms == 1000
    with pytest.raises(GatewayError) as exc:
        clock.advance_to(900)
    assert exc.value.code == errors.E_CLOCK_ROLLBACK
    assert exc.value.fields["rollback_ms"] == 100


def test_single_service_anomaly_does_not_pollute_others(loaded, tmp_path):
    # window 4 injects a persistent conn_refused fault on summarizer only;
    # chat and embed requests in the same round still succeed.
    services, nodes, trace = loaded
    store = Store(str(tmp_path / "r.db"))
    try:
        report = Engine(services, nodes, trace, store,
                        upstream=SimulatedUpstream()).run()
    finally:
        store.close()
    w4 = report["rounds"][4]
    by_service = {}
    for req in w4["requests"]:
        by_service.setdefault(req["service"], []).append(req)
    assert any(r["error_code"] == errors.E_UPSTREAM_CONN_REFUSED
               for r in by_service["summarizer"])
    assert all(r["outcome"] == "success" for r in by_service["chat"])
    assert all(r["outcome"] == "success" for r in by_service["embed"])
