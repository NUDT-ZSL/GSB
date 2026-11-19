from __future__ import annotations

import socketserver
import threading

from .conftest import call_gateway, modify_config, principals, update_raw


class _MalformedHandler(socketserver.BaseRequestHandler):
    def handle(self) -> None:
        self.request.recv(65536)
        self.request.sendall(
            b"HTTP/1.1 200 OK\r\n"
            b"Content-Type: text/plain\r\n"
            b"Content-Length: 100\r\n"
            b"Connection: close\r\n"
            b"\r\n"
            b"short"
        )


class _ReusableTCPServer(socketserver.ThreadingTCPServer):
    allow_reuse_address = True


def test_upstream_connection_refused_has_distinct_code(service):
    response = call_gateway(
        service,
        principal=principals()["api_a_v1"],
        request_id="up-refused",
        request_seq=1,
        logical_time_ms=9_000,
        path="/v1/down",
    )
    assert response.status == 502
    assert response.code == "E_UPSTREAM_REFUSED"
    upstream_stage = next(
        item for item in response.decision_chain if item["stage"] == "upstream"
    )
    assert upstream_stage["code"] == "E_UPSTREAM_REFUSED"
    assert upstream_stage["details"]["route_id"] == "down"


def test_upstream_budget_timeout_has_distinct_code(service):
    response = call_gateway(
        service,
        principal=principals()["api_a_v1"],
        request_id="up-timeout",
        request_seq=1,
        logical_time_ms=9_000,
        path="/v1/orders/slow",
        query_string="delay_ms=1000",
    )
    assert response.status == 504
    assert response.code == "E_UPSTREAM_TIMEOUT"
    upstream_stage = next(
        item for item in response.decision_chain if item["stage"] == "upstream"
    )
    assert upstream_stage["details"]["budget_ms"] == 350


def test_valid_upstream_status_is_forwarded(service):
    response = call_gateway(
        service,
        principal=principals()["api_a_v1"],
        request_id="up-503",
        request_seq=1,
        logical_time_ms=9_000,
        path="/v1/orders",
        query_string="status=503",
    )
    assert response.status == 503
    assert response.code is None
    assert any(item["stage"] == "upstream" for item in response.decision_chain)


def test_upstream_anomaly_does_not_increment_rejection_count(service):
    timeout = call_gateway(
        service,
        principal=principals()["api_a_v1"],
        request_id="isolation-1",
        request_seq=1,
        logical_time_ms=9_000,
        path="/v1/orders/slow",
        query_string="delay_ms=1000",
    )
    assert timeout.code == "E_UPSTREAM_TIMEOUT"
    state_after_timeout = service.state_snapshot("tenant-a")
    assert all(item["rejections"] == 0 for item in state_after_timeout["window_buckets"])

    healthy = call_gateway(
        service,
        principal=principals()["api_a_v1"],
        request_id="isolation-2",
        request_seq=2,
        logical_time_ms=9_001,
        path="/v1/inventory",
    )
    assert healthy.status == 200
    final_state = service.state_snapshot("tenant-a")
    assert all(item["rejections"] == 0 for item in final_state["window_buckets"])


def test_malformed_upstream_response_has_distinct_code(service):
    server = _ReusableTCPServer(("127.0.0.1", 18082), _MalformedHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        raw = modify_config(2)
        raw["routes"].append(
            {
                "id": "raw",
                "host": "api.local",
                "path_prefix": "/v1/raw",
                "upstream": "http://127.0.0.1:18082",
                "permission_domain": "raw.read",
                "request_budget_ms": 200,
            }
        )
        raw["limits"]["route_buckets"]["raw"] = {
            "capacity": 3,
            "refill_per_second": 1,
        }
        raw["auth"]["permission_matrix"]["raw.read"] = ["raw"]
        raw["auth"]["tenant_grants"]["tenant-a"].append("raw.read")
        update_raw(service, raw, expected=1)

        response = call_gateway(
            service,
            principal=principals()["api_a_v2"],
            request_id="up-malformed",
            request_seq=1,
            logical_time_ms=10_000,
            path="/v1/raw",
        )
        assert response.status == 502
        assert response.code == "E_UPSTREAM_MALFORMED"
        upstream_stage = next(
            item for item in response.decision_chain if item["stage"] == "upstream"
        )
        assert upstream_stage["details"]["reason"] in {
            "incomplete_read",
            "IncompleteRead",
            "content_length_mismatch",
        }
    finally:
        server.shutdown()
        thread.join(timeout=5)
        server.server_close()
