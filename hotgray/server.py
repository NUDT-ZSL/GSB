from __future__ import annotations

import json
import sys
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from .engine import Engine
from .errors import error_catalog
from .util import canonical_json

WEB_ROOT = Path(__file__).resolve().parent / "web"
STATIC_FILES = {
    "/": ("index.html", "text/html; charset=utf-8"),
    "/index.html": ("index.html", "text/html; charset=utf-8"),
    "/styles.css": ("styles.css", "text/css; charset=utf-8"),
    "/app.js": ("app.js", "application/javascript; charset=utf-8"),
}


class WorkbenchHandler(BaseHTTPRequestHandler):
    server_version = "HotGrayWorkbench/0.1"
    engine: Engine

    def _send_bytes(
        self,
        status: HTTPStatus,
        body: bytes,
        content_type: str,
        *,
        cache_control: str = "no-store",
    ) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", cache_control)
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(body)

    def send_response(self, code: int, message: str | None = None) -> None:
        """Send a response without the standard library's wall-clock Date header."""
        self.log_request(code)
        self.send_response_only(code, message)
        self.send_header("Server", self.version_string())

    def _send_json(self, status: HTTPStatus, payload: Any) -> None:
        body = (json.dumps(payload, ensure_ascii=False, sort_keys=True) + "\n").encode("utf-8")
        self._send_bytes(status, body, "application/json; charset=utf-8")

    def do_GET(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        path = parsed.path
        try:
            if path == "/api/status":
                self._send_json(HTTPStatus.OK, self.engine.snapshot())
                return
            if path == "/api/health":
                snapshot = self.engine.snapshot()
                self._send_json(
                    HTTPStatus.OK,
                    {
                        "ok": True,
                        "status": snapshot["round"]["status"],
                        "logical_watermark_ms": snapshot["logical_clock"]["watermark_ms"],
                        "round_id": snapshot["round"]["round_id"],
                    },
                )
                return
            if path == "/api/error-catalog":
                self._send_json(
                    HTTPStatus.OK,
                    {"ok": True, "errors": error_catalog()},
                )
                return
            if path in STATIC_FILES:
                filename, content_type = STATIC_FILES[path]
                file_path = WEB_ROOT / filename
                if not file_path.is_file():
                    self._send_json(
                        HTTPStatus.NOT_FOUND,
                        {"ok": False, "error": {"code": "HG1017", "message": "静态资源缺失"}},
                    )
                    return
                self._send_bytes(
                    HTTPStatus.OK,
                    file_path.read_bytes(),
                    content_type,
                    cache_control="no-cache",
                )
                return
            self._send_json(
                HTTPStatus.NOT_FOUND,
                {"ok": False, "error": {"code": "HG1017", "message": f"路径不存在: {path}"}},
            )
        except Exception as exc:  # pragma: no cover - defensive HTTP boundary
            self._send_json(
                HTTPStatus.INTERNAL_SERVER_ERROR,
                {
                    "ok": False,
                    "error": {
                        "code": "HG1019",
                        "message": f"诊断服务内部错误: {type(exc).__name__}",
                    },
                },
            )

    def do_HEAD(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        if parsed.path in STATIC_FILES:
            filename, content_type = STATIC_FILES[parsed.path]
            path = WEB_ROOT / filename
            if path.is_file():
                self.send_response(HTTPStatus.OK)
                self.send_header("Content-Type", content_type)
                self.send_header("Content-Length", str(path.stat().st_size))
                self.end_headers()
                return
        self.send_response(HTTPStatus.NOT_FOUND)
        self.end_headers()

    def log_message(self, format: str, *args: Any) -> None:
        sys.stderr.write(
            canonical_json(
                {
                    "http": "request",
                    "method": self.command,
                    "path": self.path,
                    "message": format % args,
                }
            )
            + "\n"
        )


def make_server(
    db_path: str | Path,
    *,
    host: str = "127.0.0.1",
    port: int = 8088,
) -> ThreadingHTTPServer:
    engine = Engine.open(db_path)
    handler = type("BoundWorkbenchHandler", (WorkbenchHandler,), {"engine": engine})
    server = ThreadingHTTPServer((host, port), handler)
    server.daemon_threads = True
    return server


def serve(
    db_path: str | Path,
    *,
    host: str = "127.0.0.1",
    port: int = 8088,
) -> None:
    server = make_server(db_path, host=host, port=port)
    print(
        canonical_json(
            {
                "ok": True,
                "event": "workbench_listening",
                "host": host,
                "port": port,
                "url": f"http://{host}:{port}/",
            }
        ),
        flush=True,
    )
    try:
        server.serve_forever(poll_interval=0.5)
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
