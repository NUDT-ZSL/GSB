from __future__ import annotations

import json
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from .diagnostics import build_diagnostics, ensure_selfcheck, render_dashboard_html


def make_handler(stage_dir: Path | str) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        def _send_json(self, value: Any, status: int = HTTPStatus.OK) -> None:
            body = json.dumps(value, ensure_ascii=False, sort_keys=True).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def _send_html(self, value: str, status: int = HTTPStatus.OK) -> None:
            body = value.encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self) -> None:
            path = urlparse(self.path).path
            try:
                if path in {"/", "/index.html"}:
                    self._send_html(render_dashboard_html(build_diagnostics(stage_dir)))
                    return
                if path == "/api/diagnostics":
                    self._send_json(build_diagnostics(stage_dir))
                    return
                if path == "/api/selfcheck":
                    self._send_json(ensure_selfcheck(stage_dir))
                    return
                if path == "/api/errors":
                    from .store import Stage

                    self._send_json({"errors": Stage(stage_dir).recent_errors()})
                    return
                self._send_json(
                    {"error": "not found", "path": path},
                    status=HTTPStatus.NOT_FOUND,
                )
            except Exception as exc:  # pragma: no cover - defensive HTTP boundary
                self._send_json(
                    {
                        "error": type(exc).__name__,
                        "message": str(exc),
                    },
                    status=HTTPStatus.INTERNAL_SERVER_ERROR,
                )

        def log_message(self, format: str, *args: Any) -> None:
            return

    return Handler


def serve(stage_dir: Path | str, *, host: str = "127.0.0.1", port: int = 8765) -> None:
    server = ThreadingHTTPServer((host, port), make_handler(stage_dir))
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
