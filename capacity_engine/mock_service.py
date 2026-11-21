from __future__ import annotations

import argparse
import json
import os
import socket
import struct
import sys
from decimal import Decimal
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlsplit

from .util import json_default, pretty_json


class MockConfig:
    def __init__(self, document: dict[str, Any]) -> None:
        self.document = document
        self.tenants = document.get("tenants", {})
        if not isinstance(self.tenants, dict):
            raise ValueError("mock config tenants must be an object")

    def tenant(self, name: str) -> dict[str, Any] | None:
        value = self.tenants.get(name)
        return value if isinstance(value, dict) else None


def select_response(
    tenant_config: dict[str, Any],
    gradient_index: int,
    concurrency: int,
    request_index: int,
) -> dict[str, Any]:
    rule: Any = None
    by_gradient = tenant_config.get("by_gradient_index", {})
    if isinstance(by_gradient, dict) and str(gradient_index) in by_gradient:
        rule = by_gradient[str(gradient_index)]
    if rule is None:
        by_concurrency = tenant_config.get("by_concurrency", {})
        if isinstance(by_concurrency, dict) and str(concurrency) in by_concurrency:
            rule = by_concurrency[str(concurrency)]
    if rule is None:
        rule = tenant_config.get("default", tenant_config.get("response"))
    if rule is None:
        rule = {"status": 200, "latency_us": 10000}
    response = _select_from_rule(rule, request_index)
    if not isinstance(response, dict):
        raise ValueError("mock response rule must resolve to an object")
    return response


def _select_from_rule(rule: Any, request_index: int) -> Any:
    if isinstance(rule, list):
        if not rule:
            raise ValueError("mock response sequence must not be empty")
        return rule[request_index % len(rule)]
    if not isinstance(rule, dict):
        raise ValueError("mock response rule must be an object or array")
    if "sequence" in rule:
        sequence = rule["sequence"]
        if not isinstance(sequence, list) or not sequence:
            raise ValueError("mock response sequence must be a non-empty array")
        return sequence[request_index % len(sequence)]
    if "distribution" in rule:
        distribution = rule["distribution"]
        if not isinstance(distribution, list) or not distribution:
            raise ValueError("mock response distribution must be a non-empty array")
        total_weight = 0
        normalized: list[tuple[int, dict[str, Any]]] = []
        for entry in distribution:
            if not isinstance(entry, dict):
                raise ValueError("mock distribution entries must be objects")
            weight = entry.get("weight", 1)
            if not isinstance(weight, int) or isinstance(weight, bool) or weight <= 0:
                raise ValueError("mock distribution weight must be a positive integer")
            total_weight += weight
            normalized.append((total_weight, entry))
        position = request_index % total_weight
        for upper_bound, entry in normalized:
            if position < upper_bound:
                return entry
        return normalized[-1][1]
    return rule


def make_handler(config: MockConfig):
    class MockHandler(BaseHTTPRequestHandler):
        server_version = "CapacityMock/1.0"

        def do_GET(self) -> None:
            parsed = urlsplit(self.path)
            if parsed.path == "/health":
                self._send_json(200, {"ok": True, "service": "capacity-mock"})
                return
            parts = [unquote(part) for part in parsed.path.strip("/").split("/") if part]
            if len(parts) != 3 or parts[0] != "tenant" or parts[2] != "request":
                self._send_json(404, {"ok": False, "error": "not_found", "path": parsed.path})
                return
            tenant_name = parts[1]
            tenant_config = config.tenant(tenant_name)
            if tenant_config is None:
                self._send_json(404, {"ok": False, "error": "unknown_tenant", "tenant": tenant_name})
                return
            gradient_index = _header_int(self, "X-Gradient-Index", 0)
            concurrency = _header_int(self, "X-Concurrency", 1)
            request_index = _header_int(self, "X-Request-Index", 0)
            wave_index = _header_int(self, "X-Wave-Index", 0)
            logical_ms = _header_int(self, "X-Logical-Ms", 0)
            try:
                response = select_response(tenant_config, gradient_index, concurrency, request_index)
            except ValueError as exc:
                self._send_json(500, {"ok": False, "error": "invalid_mock_rule", "detail": str(exc)})
                return
            if response.get("connection_refused"):
                self.close_connection = True
                try:
                    self.connection.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER, struct.pack("ii", 1, 0))
                    self.connection.shutdown(socket.SHUT_RDWR)
                except OSError:
                    pass
                self.connection.close()
                return
            status = int(response.get("status", 200))
            latency_us = response.get("latency_us", 10000)
            try:
                latency_value = int(Decimal(str(latency_us)))
            except Exception:
                self._send_json(500, {"ok": False, "error": "invalid_latency", "value": str(latency_us)})
                return
            if response.get("malformed"):
                self._send_raw(
                    status,
                    "not-json-response",
                    content_type="text/plain",
                    latency_us=latency_value,
                )
                return
            if response.get("empty_body"):
                self._send_raw(
                    status,
                    "",
                    content_type="application/json",
                    latency_us=latency_value,
                )
                return
            payload = {
                "ok": 200 <= status < 300,
                "tenant": tenant_name,
                "http_status": status,
                "simulated_latency_us": latency_value,
                "gradient_index": gradient_index,
                "concurrency": concurrency,
                "wave_index": wave_index,
                "request_index": request_index,
                "logical_ms": logical_ms,
            }
            self._send_json(status, payload, latency_us=latency_value)

        def log_message(self, format: str, *args: Any) -> None:
            return

        def _send_json(self, status: int, payload: dict[str, Any], latency_us: int | None = None) -> None:
            body = json.dumps(payload, ensure_ascii=False, sort_keys=True, default=json_default).encode("utf-8")
            self._send_raw(status, body, content_type="application/json", latency_us=latency_us)

        def _send_raw(
            self,
            status: int,
            body: str | bytes,
            content_type: str = "application/json",
            latency_us: int | None = None,
        ) -> None:
            data = body.encode("utf-8") if isinstance(body, str) else body
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(data)))
            if latency_us is not None:
                self.send_header("X-Simulated-Latency-Us", str(latency_us))
            self.end_headers()
            self.wfile.write(data)

    return MockHandler


def _header_int(handler: BaseHTTPRequestHandler, name: str, default: int) -> int:
    value = handler.headers.get(name)
    if value is None:
        return default
    try:
        return int(value)
    except ValueError:
        return default


def load_mock_config(path: Path) -> MockConfig:
    with path.open("r", encoding="utf-8") as handle:
        document = json.load(handle, parse_float=Decimal)
    if not isinstance(document, dict):
        raise ValueError("mock config root must be an object")
    return MockConfig(document)


def serve(config: MockConfig, host: str, port: int) -> ThreadingHTTPServer:
    server = ThreadingHTTPServer((host, port), make_handler(config))
    server.daemon_threads = True
    return server


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Deterministic loopback HTTP test service")
    parser.add_argument("--config", required=True, help="mock response JSON configuration")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=18080)
    args = parser.parse_args(argv)
    config = load_mock_config(Path(args.config))
    server = serve(config, args.host, args.port)
    actual_port = server.server_address[1]
    print(pretty_json({"ok": True, "host": args.host, "port": actual_port, "pid": os.getpid()}), flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(main())

