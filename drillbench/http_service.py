from __future__ import annotations

import json
import socket
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

from .models import Route
from .util import sha256_text


class _ReusableThreadingHTTPServer(ThreadingHTTPServer):
    allow_reuse_address = True
    daemon_threads = True


class _Handler(BaseHTTPRequestHandler):
    server_version = "DrillBench"
    sys_version = ""
    protocol_version = "HTTP/1.0"

    def log_message(self, format: str, *args: Any) -> None:
        return

    def date_time_string(self, timestamp: float | None = None) -> str:
        return "Thu, 01 Jan 1970 00:00:00 GMT"

    def do_GET(self) -> None:
        route_map: dict[str, Route] = self.server.route_map  # type: ignore[attr-defined]
        route = route_map.get(self.path)
        if route is None:
            body = b'{"error":"not_found"}'
            self.send_response(404)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Connection", "close")
            self.end_headers()
            self.wfile.write(body)
            return

        injection_types = [
            value.strip()
            for value in self.headers.get("X-Drill-Injection-Types", "").split(",")
            if value.strip()
        ]
        if "network_outage" in injection_types:
            self._drop_connection()
            return

        effect = "none"
        status = route.status
        body_text = route.body
        content_type = route.content_type
        if "service_reject" in injection_types:
            status = 503
            effect = "service_reject"
            content_type = "application/json"
            body_text = json.dumps(
                {
                    "error": "service_rejected",
                    "injection": "service_reject",
                },
                sort_keys=True,
                separators=(",", ":"),
            )

        try:
            real_delay_ms = int(
                self.headers.get("X-Drill-Delay-Real-Ms", "0") or "0"
            )
        except ValueError:
            real_delay_ms = 0
        if real_delay_ms > 0 and effect != "service_reject":
            time.sleep(real_delay_ms / 1000.0)

        if route.mode == "disconnect":
            self._drop_connection()
            return

        if "data_corruption" in injection_types:
            effect = "data_corruption"
            body_text = body_text + "\x00drill-corruption"

        if route.mode == "malformed":
            body_text = '{"broken":'
            content_type = "application/json"

        body = body_text.encode("utf-8")
        try:
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Connection", "close")
            self.send_header("X-Drill-Effect", effect)
            self.send_header("X-Drill-Body-SHA256", sha256_text(body_text))
            self.end_headers()
            self.wfile.write(body)
            self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError, OSError):
            return

    def _drop_connection(self) -> None:
        try:
            self.connection.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        try:
            self.connection.close()
        except OSError:
            pass
        self.close_connection = True


class LocalHTTPService:
    def __init__(self, routes: tuple[Route, ...]) -> None:
        self._server = _ReusableThreadingHTTPServer(
            ("127.0.0.1", 0),
            _Handler,
        )
        self._server.route_map = {route.path: route for route in routes}  # type: ignore[attr-defined]
        self._thread = threading.Thread(
            target=lambda: self._server.serve_forever(poll_interval=0.01),
            name="drillbench-http",
            daemon=True,
        )
        self._started = False

    @property
    def host(self) -> str:
        return "127.0.0.1"

    @property
    def port(self) -> int:
        return int(self._server.server_address[1])

    def start(self) -> "LocalHTTPService":
        if not self._started:
            self._thread.start()
            self._started = True
        return self

    def close(self) -> None:
        if self._started:
            self._server.shutdown()
            self._server.server_close()
            self._thread.join(timeout=2.0)
            self._started = False

    def __enter__(self) -> "LocalHTTPService":
        return self.start()

    def __exit__(self, exc_type: object, exc: object, traceback: object) -> None:
        self.close()

