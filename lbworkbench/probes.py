from __future__ import annotations

import ipaddress
import json
import socket
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import urljoin, urlparse

import requests

from .errors import WorkbenchError


class ProbeHTTPServer:
    """Deterministic local loopback probe fixture."""

    def __init__(self) -> None:
        self._server = ThreadingHTTPServer(("127.0.0.1", 0), _ProbeHandler)
        self._server.daemon_threads = True
        self._thread = threading.Thread(
            target=self._server.serve_forever,
            name="lb-probe-fixture",
            daemon=True,
        )

    @property
    def port(self) -> int:
        return int(self._server.server_address[1])

    @property
    def base_url(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    def start(self) -> "ProbeHTTPServer":
        self._thread.start()
        return self

    def stop(self) -> None:
        self._server.shutdown()
        self._server.server_close()
        self._thread.join(timeout=5)

    def __enter__(self) -> "ProbeHTTPServer":
        return self.start()

    def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        self.stop()


class _ProbeHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, format: str, *args: Any) -> None:
        return

    def _send_json(self, status: int, payload: Any) -> None:
        body = json.dumps(payload, sort_keys=True).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.write(body)
        self.close_connection = True

    def do_GET(self) -> None:
        if self.path == "/ok":
            self._send_json(200, {"ok": True})
            return
        if self.path == "/fail":
            self._send_json(503, {"ok": False, "reason": "injected_status_mismatch"})
            return
        if self.path == "/slow":
            time.sleep(0.6)
            self._send_json(200, {"ok": True, "slow": True})
            return
        if self.path == "/malformed":
            body = b'{"broken":'
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Connection", "close")
            self.end_headers()
            self.wfile.write(body)
            self.close_connection = True
            return
        if self.path == "/reset":
            self.close_connection = True
            try:
                self.connection.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            try:
                self.connection.close()
            except OSError:
                pass
            return
        self._send_json(404, {"ok": False, "reason": "not_found"})


def _resolve_target(base_url: str | None, target: str) -> str:
    if target.startswith(("http://", "https://")):
        return target
    if not base_url:
        raise WorkbenchError(
            "E_BAD_PROBE_TARGET",
            "probe_base_url is required for relative probe targets",
            {"target": target},
        )
    return urljoin(base_url.rstrip("/") + "/", target.lstrip("/"))


def _is_loopback_host(hostname: str) -> bool:
    if hostname.lower() == "localhost":
        return True
    try:
        return ipaddress.ip_address(hostname).is_loopback
    except ValueError:
        return False


def _validate_loopback(url: str) -> None:
    parsed = urlparse(url)
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.hostname
        or not _is_loopback_host(parsed.hostname)
    ):
        raise WorkbenchError(
            "E_BAD_PROBE_TARGET",
            "only loopback probe targets are permitted",
            {"target_host": parsed.hostname, "scheme": parsed.scheme},
        )


def execute_probe(probe: dict[str, Any], base_url: str | None) -> dict[str, Any]:
    target = probe["target"]
    url = _resolve_target(base_url, target)
    _validate_loopback(url)
    timeout_seconds = int(probe["timeout_ms"]) / 1000.0
    result: dict[str, Any] = {
        "instance_id": probe["instance_id"],
        "target": target,
        "target_mode": "absolute_url" if target.startswith(("http://", "https://")) else "relative",
        "expected_status": probe["expected_status"],
        "require_json": probe["require_json"],
        "request_budget_ms": probe["timeout_ms"],
        "http_status": None,
        "error_code": None,
        "observed_health": "unhealthy",
        "detail": "",
    }
    session = requests.Session()
    session.trust_env = False
    try:
        response = session.get(url, timeout=timeout_seconds, allow_redirects=False)
        result["http_status"] = response.status_code
        if response.status_code != probe["expected_status"]:
            result["error_code"] = "E_PROBE_STATUS_MISMATCH"
            result["detail"] = (
                f"received HTTP {response.status_code}, expected {probe['expected_status']}"
            )
            return result
        if probe["require_json"]:
            try:
                response.json()
            except (ValueError, json.JSONDecodeError):
                result["error_code"] = "E_PROBE_MALFORMED"
                result["detail"] = "response body is not valid JSON"
                return result
        result["observed_health"] = "healthy"
        result["detail"] = "probe matched expected status and response shape"
        return result
    except requests.Timeout:
        result["error_code"] = "E_PROBE_TIMEOUT"
        result["detail"] = f"response exceeded {probe['timeout_ms']} ms probe budget"
        return result
    except requests.ConnectionError:
        result["error_code"] = "E_PROBE_REFUSED"
        result["detail"] = "connection was refused or reset"
        return result
    except requests.RequestException as exc:
        result["error_code"] = "E_PROBE_TRANSPORT"
        result["detail"] = f"transport error: {type(exc).__name__}"
        return result
    finally:
        session.close()
