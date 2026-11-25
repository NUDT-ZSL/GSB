from __future__ import annotations

import json
import threading
import webbrowser
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from .errors import BusinessError, E_BAD_REQUEST
from .service import ImageDiagService


STATIC_ROOT = Path(__file__).resolve().parent / "static"


class DashboardHandler(BaseHTTPRequestHandler):
    server_version = "RepairGraph/1.0"
    service: ImageDiagService

    def log_message(self, format: str, *args: Any) -> None:
        if not self.path.startswith("/static/"):
            super().log_message(format, *args)

    def _send_json(self, status: int, payload: dict[str, Any]) -> None:
        encoded = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(encoded)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(encoded)

    def _send_bytes(self, status: int, content_type: str, payload: bytes) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(payload)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(payload)

    def _read_json(self) -> dict[str, Any]:
        length = int(self.headers.get("Content-Length", "0"))
        if length == 0:
            return {}
        raw = self.rfile.read(length)
        try:
            value = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise BusinessError(
                E_BAD_REQUEST,
                "request body must be valid JSON",
                {"reason": str(exc)},
            ) from exc
        if not isinstance(value, dict):
            raise BusinessError(E_BAD_REQUEST, "request body must be a JSON object")
        return value

    def _dispatch(self, action: Any) -> None:
        try:
            payload = action()
            self._send_json(HTTPStatus.OK, payload)
        except BusinessError as exc:
            self._send_json(exc.http_status, exc.as_dict())
        except Exception as exc:
            self._send_json(
                HTTPStatus.INTERNAL_SERVER_ERROR,
                {
                    "ok": False,
                    "error": {
                        "code": "E_INTERNAL",
                        "message": str(exc),
                        "details": {},
                    },
                },
            )

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        if parsed.path in ("/", "/index.html"):
            self._send_bytes(
                HTTPStatus.OK,
                "text/html; charset=utf-8",
                (STATIC_ROOT / "index.html").read_bytes(),
            )
            return
        if parsed.path == "/static/styles.css":
            self._send_bytes(
                HTTPStatus.OK,
                "text/css; charset=utf-8",
                (STATIC_ROOT / "styles.css").read_bytes(),
            )
            return
        if parsed.path == "/static/app.js":
            self._send_bytes(
                HTTPStatus.OK,
                "application/javascript; charset=utf-8",
                (STATIC_ROOT / "app.js").read_bytes(),
            )
            return
        if parsed.path == "/api/state":
            self._dispatch(self.service.state_payload)
            return
        if parsed.path == "/favicon.ico":
            self._send_bytes(HTTPStatus.NO_CONTENT, "image/x-icon", b"")
            return
        self._send_json(
            HTTPStatus.NOT_FOUND,
            {
                "ok": False,
                "error": {"code": "E_NOT_FOUND", "message": "not found", "details": {}},
            },
        )

    def do_POST(self) -> None:
        parsed = urlparse(self.path)
        if parsed.path == "/api/run":
            body = self._read_json()
            self._dispatch(
                lambda: self.service.run_compare(
                    precision=str(body.get("precision", "float64")),
                    focus_path=str(body.get("focus_path", "block")),
                    policy=body.get("policy"),
                    threshold_tolerance=(
                        float(body["threshold_tolerance"])
                        if body.get("threshold_tolerance") is not None
                        else None
                    ),
                    error_tolerance=(
                        float(body["error_tolerance"])
                        if body.get("error_tolerance") is not None
                        else None
                    ),
                )
            )
            return
        if parsed.path == "/api/demo/incremental":
            self._dispatch(self.service.demo_incremental)
            return
        if parsed.path == "/api/demo/style":
            self._dispatch(self.service.demo_style_revision)
            return
        if parsed.path == "/api/demo/rollback":
            self._dispatch(self.service.demo_rollback)
            return
        if parsed.path == "/api/demo/busy":
            self._dispatch(self.service.demo_busy)
            return
        if parsed.path == "/api/demo/recovery":
            self._dispatch(self.service.demo_recovery)
            return
        if parsed.path == "/api/reset":
            self._dispatch(self.service.reset_runtime_to_sample)
            return
        self._send_json(
            HTTPStatus.NOT_FOUND,
            {
                "ok": False,
                "error": {"code": "E_NOT_FOUND", "message": "not found", "details": {}},
            },
        )


def serve(
    *,
    host: str,
    port: int,
    workspace: str | Path,
    sample_dir: str | Path,
    open_browser: bool = True,
) -> int:
    service = ImageDiagService(workspace, sample_dir)

    class Handler(DashboardHandler):
        pass

    Handler.service = service
    server = ThreadingHTTPServer((host, port), Handler)
    server.daemon_threads = True
    url = f"http://{host}:{port}/"
    print(f"RepairGraph dashboard: {url}", flush=True)
    print(f"Workspace: {service.workspace.resolve()}", flush=True)
    if open_browser:
        threading.Timer(0.35, lambda: webbrowser.open(url)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopping dashboard.", flush=True)
    finally:
        server.server_close()
    return 0
