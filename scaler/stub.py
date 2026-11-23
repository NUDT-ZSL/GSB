from __future__ import annotations

import json
import threading
import time
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

DEFAULT_BEHAVIOR: dict[str, Any] = {"mode": "success", "delay_ms": 0}


class StubState:
    def __init__(self, scenario: dict[str, Any] | None = None) -> None:
        self.scenario = scenario or {}
        self.behaviors = self.scenario.get("services", self.scenario)
        self.operations: dict[str, dict[str, Any]] = {}
        self.service_attempts: dict[str, int] = {}
        self.in_progress: dict[str, threading.Event] = {}
        self.lock = threading.Lock()

    def behavior_for(self, service: str) -> dict[str, Any]:
        value = self.behaviors.get(service, DEFAULT_BEHAVIOR)
        if not isinstance(value, dict):
            return dict(DEFAULT_BEHAVIOR)
        merged = dict(DEFAULT_BEHAVIOR)
        merged.update(value)
        return merged

    def get_operation(self, operation_id: str) -> dict[str, Any] | None:
        with self.lock:
            return self.operations.get(operation_id)


def make_handler(state: StubState) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        server_version = "DeterministicLaunchStub/1.0"

        def log_message(self, fmt: str, *args: Any) -> None:
            return

        def _send_json(self, status: int, payload: dict[str, Any]) -> None:
            body = json.dumps(payload, sort_keys=True).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Connection", "close")
            self.end_headers()
            try:
                self.wfile.write(body)
            except (BrokenPipeError, ConnectionResetError):
                return

        def _read_json(self) -> dict[str, Any]:
            length = int(self.headers.get("Content-Length", "0"))
            raw = self.rfile.read(length)
            try:
                value = json.loads(raw.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError):
                self._send_json(
                    HTTPStatus.BAD_REQUEST,
                    {"ok": False, "error_code": "E_INPUT_INVALID"},
                )
                raise ValueError("invalid JSON")
            if not isinstance(value, dict):
                self._send_json(
                    HTTPStatus.BAD_REQUEST,
                    {"ok": False, "error_code": "E_INPUT_INVALID"},
                )
                raise ValueError("payload must be object")
            return value

        def do_POST(self) -> None:
            parsed = urlparse(self.path)
            if parsed.path == "/reset":
                with state.lock:
                    state.operations.clear()
                    state.service_attempts.clear()
                    state.in_progress.clear()
                self._send_json(HTTPStatus.OK, {"ok": True})
                return
            if parsed.path != "/launch":
                self._send_json(
                    HTTPStatus.NOT_FOUND,
                    {"ok": False, "error_code": "E_NOT_FOUND"},
                )
                return
            try:
                payload = self._read_json()
            except ValueError:
                return
            operation_id = payload.get("operation_id")
            service = payload.get("service")
            if not isinstance(operation_id, str) or not operation_id:
                self._send_json(
                    HTTPStatus.BAD_REQUEST,
                    {"ok": False, "error_code": "E_INPUT_INVALID"},
                )
                return
            if not isinstance(service, str) or not service:
                self._send_json(
                    HTTPStatus.BAD_REQUEST,
                    {"ok": False, "error_code": "E_INPUT_INVALID"},
                )
                return
            with state.lock:
                existing = state.operations.get(operation_id)
                in_progress = state.in_progress.get(operation_id)
                owner = existing is None and in_progress is None
                if owner:
                    in_progress = threading.Event()
                    state.in_progress[operation_id] = in_progress
            if existing is not None:
                self._send_json(HTTPStatus.OK, existing)
                return
            if not owner:
                assert in_progress is not None
                in_progress.wait(timeout=35.0)
                existing = state.get_operation(operation_id)
                if existing is not None:
                    self._send_json(HTTPStatus.OK, existing)
                else:
                    self._send_json(
                        HTTPStatus.SERVICE_UNAVAILABLE,
                        {
                            "ok": False,
                            "operation_id": operation_id,
                            "error_code": "E_STUB_UNAVAILABLE",
                            "message": "operation is still in progress",
                        },
                    )
                return
            behavior = state.behavior_for(service)
            mode = behavior.get("mode", "success")
            with state.lock:
                attempt = state.service_attempts.get(service, 0) + 1
                state.service_attempts[service] = attempt
            delay_ms = behavior.get("delay_ms", 0)
            if isinstance(delay_ms, bool) or not isinstance(delay_ms, (int, float)):
                delay_ms = 0
            delay_ms = max(0, min(30000, int(delay_ms)))
            if delay_ms:
                time.sleep(delay_ms / 1000.0)
            try:
                effective_mode = mode
                effective_behavior = behavior
                if mode == "fail_first_n":
                    failures = int(behavior.get("failures", 1))
                    if attempt > failures:
                        effective_behavior = dict(
                            behavior.get("then", {"mode": "success"})
                        )
                        effective_mode = effective_behavior.get("mode", "success")
                        delay_ms = effective_behavior.get("delay_ms", delay_ms)
                if effective_mode == "http_500":
                    response = {
                        "ok": False,
                        "operation_id": operation_id,
                        "error_code": "E_LAUNCH_HTTP_ERROR",
                        "duration_ms": delay_ms,
                    }
                    status = HTTPStatus.INTERNAL_SERVER_ERROR
                elif effective_mode == "fail" or (
                    mode == "fail_first_n"
                    and attempt <= int(behavior.get("failures", 1))
                ):
                    response = {
                        "ok": False,
                        "operation_id": operation_id,
                        "error_code": str(
                            effective_behavior.get(
                                "error_code", "E_LAUNCH_HTTP_ERROR"
                            )
                        ),
                        "message": str(
                            behavior.get("message", "deterministic stub failure")
                        ),
                        "duration_ms": delay_ms,
                    }
                    status = HTTPStatus.OK
                else:
                    response = {
                        "ok": True,
                        "operation_id": operation_id,
                        "service": service,
                        "replica_id": payload.get("replica_id"),
                        "status": "ready",
                        "duration_ms": delay_ms,
                        "health_fail": effective_mode == "health_fail",
                    }
                    status = HTTPStatus.OK
                with state.lock:
                    state.operations[operation_id] = response
            finally:
                with state.lock:
                    state.in_progress.pop(operation_id, None)
                    in_progress.set()
            self._send_json(status, response)

        def do_GET(self) -> None:
            parsed = urlparse(self.path)
            if parsed.path == "/healthz":
                self._send_json(HTTPStatus.OK, {"ok": True})
                return
            if parsed.path == "/stats":
                with state.lock:
                    payload = {
                        "ok": True,
                        "operation_count": len(state.operations),
                        "operation_ids": sorted(state.operations),
                    }
                self._send_json(HTTPStatus.OK, payload)
                return
            if parsed.path.startswith("/operations/"):
                operation_id = parsed.path[len("/operations/") :]
                existing = state.get_operation(operation_id)
                if existing is None:
                    self._send_json(
                        HTTPStatus.NOT_FOUND,
                        {"ok": False, "error_code": "E_OPERATION_NOT_FOUND"},
                    )
                    return
                self._send_json(HTTPStatus.OK, existing)
                return
            if parsed.path == "/health":
                query = parse_qs(parsed.query)
                operation_id = (query.get("operation_id") or [""])[0]
                existing = state.get_operation(operation_id)
                if existing is None:
                    self._send_json(
                        HTTPStatus.NOT_FOUND,
                        {"ok": False, "error_code": "E_OPERATION_NOT_FOUND"},
                    )
                    return
                if existing.get("health_fail"):
                    self._send_json(
                        HTTPStatus.OK,
                        {
                            "ok": False,
                            "operation_id": operation_id,
                            "error_code": "E_HEALTH_PROBE_FAILED",
                            "message": "deterministic health probe failure",
                        },
                    )
                else:
                    self._send_json(
                        HTTPStatus.OK,
                        {"ok": True, "operation_id": operation_id},
                    )
                return
            self._send_json(
                HTTPStatus.NOT_FOUND,
                {"ok": False, "error_code": "E_NOT_FOUND"},
            )

    return Handler


class StubServer:
    def __init__(
        self,
        host: str = "127.0.0.1",
        port: int = 0,
        scenario: dict[str, Any] | None = None,
    ) -> None:
        self.state = StubState(scenario)
        self.httpd = ThreadingHTTPServer((host, port), make_handler(self.state))
        self.httpd.daemon_threads = True
        self.thread = threading.Thread(
            target=self.httpd.serve_forever, name="launch-stub", daemon=True
        )

    @property
    def port(self) -> int:
        return int(self.httpd.server_address[1])

    @property
    def url(self) -> str:
        host, port = self.httpd.server_address[:2]
        return f"http://{host}:{port}"

    def start(self) -> StubServer:
        self.thread.start()
        return self

    def stop(self) -> None:
        self.httpd.shutdown()
        self.httpd.server_close()
        self.thread.join(timeout=3.0)

    def __enter__(self) -> StubServer:
        return self.start()

    def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        self.stop()


def load_scenario(path: str | Path | None) -> dict[str, Any]:
    if path is None:
        return {}
    return json.loads(Path(path).read_text(encoding="utf-8"))
