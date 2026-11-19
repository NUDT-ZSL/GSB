"""Loopback HTTP test target simulating an action-dispatch endpoint.

Modes (switched via POST /ctl, no external network involved):
  ok            -> 200 with the expected version echo
  bad_version   -> 200 with a mismatched version echo
  timeout       -> sleeps beyond the client timeout
  unhealthy     -> /health reports not-ready
Connection-refused is simulated by pointing the client at a closed port.
The server applies each (service, action_id, version) idempotently and
counts applies so tests can prove no duplicate commits happen.
"""

from __future__ import annotations

import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


class _State:
    def __init__(self) -> None:
        self.mode = "ok"
        self.healthy = True
        self.timeout_seconds = 5.0
        self.applied: dict = {}
        self.apply_count = 0
        self.lock = threading.Lock()


class _Handler(BaseHTTPRequestHandler):
    server_version = "RecoveryTarget/0.1"

    def log_message(self, *args):
        pass

    def _send(self, code: int, obj: dict) -> None:
        body = json.dumps(obj).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        state: _State = self.server.state
        if self.path.startswith("/health"):
            with state.lock:
                ok = state.healthy and state.mode != "unhealthy"
            self._send(200 if ok else 503, {"healthy": ok})
        elif self.path.startswith("/stats"):
            with state.lock:
                self._send(200, {"apply_count": state.apply_count,
                                 "applied_keys": sorted(state.applied)})
        else:
            self._send(404, {"error": "not_found"})

    def do_POST(self):
        state: _State = self.server.state
        length = int(self.headers.get("Content-Length", "0"))
        payload = json.loads(self.rfile.read(length) or b"{}")
        if self.path.startswith("/ctl"):
            with state.lock:
                state.mode = payload.get("mode", state.mode)
                if "healthy" in payload:
                    state.healthy = bool(payload["healthy"])
            self._send(200, {"ok": True})
            return
        if not self.path.startswith("/action"):
            self._send(404, {"error": "not_found"})
            return

        with state.lock:
            mode = state.mode
        if mode == "timeout":
            time.sleep(state.timeout_seconds)
        key = (payload.get("service"), payload.get("action_id"),
               payload.get("version"))
        with state.lock:
            if key not in state.applied:
                state.applied[key] = payload
                state.apply_count += 1
        echo = payload.get("version")
        if mode == "bad_version":
            echo = f"{echo}-STALE"
        self._send(200, {"echo_version": echo, "applied": True})


class TargetServer:
    """Context manager running the loopback target on an ephemeral port."""

    def __init__(self) -> None:
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
        self.httpd.state = _State()
        self.port = self.httpd.server_address[1]
        self._thread = threading.Thread(target=self.httpd.serve_forever,
                                        daemon=True)

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    def __enter__(self) -> "TargetServer":
        self._thread.start()
        return self

    def __exit__(self, *exc) -> None:
        self.httpd.shutdown()
        self.httpd.server_close()
        self._thread.join(timeout=5)
