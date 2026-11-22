"""Zero-dependency local HTTP application for the demonstration dashboard."""

from __future__ import annotations

import json
import threading
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from importlib import resources
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

from .cache import ArtifactCache
from .config import PROJECT_ROOT
from .demo import run_demo
from .errors import BusinessError, ErrorCode


def _error_status(code: str) -> int:
    if code == ErrorCode.STAGE_BUSY:
        return HTTPStatus.LOCKED
    if code in {
        ErrorCode.SNAPSHOT_ROLLBACK,
        ErrorCode.SNAPSHOT_CORRUPT,
        ErrorCode.CACHE_CORRUPT,
    }:
        return HTTPStatus.CONFLICT
    return HTTPStatus.UNPROCESSABLE_ENTITY


class DemoController:
    def __init__(
        self,
        *,
        cache_dir: str | Path | None = None,
        artifact_path: str | Path | None = None,
    ):
        self.cache_dir = (
            Path(cache_dir)
            if cache_dir is not None
            else PROJECT_ROOT / ".cache" / "flightfusion-app"
        )
        self.artifact_path = (
            Path(artifact_path)
            if artifact_path is not None
            else PROJECT_ROOT / "artifacts" / "live_demo.json"
        )
        self._report: dict[str, Any] | None = None
        self._lock = threading.Lock()

    def report(self) -> dict[str, Any]:
        with self._lock:
            if self._report is None:
                self._report = run_demo(
                    cache_dir=self.cache_dir,
                    artifact_path=self.artifact_path,
                    fresh_cache=False,
                )
            return self._report

    def refresh(
        self,
        *,
        injections: list[str | dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        with self._lock:
            self._report = run_demo(
                cache_dir=self.cache_dir,
                artifact_path=self.artifact_path,
                extra_injections=injections or None,
                fresh_cache=False,
            )
            return self._report


def _make_handler(controller: DemoController):
    class Handler(BaseHTTPRequestHandler):
        server_version = "FlightFusion/0.1"

        def log_message(self, format: str, *args: Any) -> None:
            print(f"[flightfusion] {self.address_string()} {format % args}")

        def _json(self, payload: Any, status: int = HTTPStatus.OK) -> None:
            data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(data)

        def _html(self) -> None:
            html = (
                resources.files("flightfusion")
                .joinpath("web/index.html")
                .read_text(encoding="utf-8")
            ).encode("utf-8")
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(html)))
            self.end_headers()
            self.wfile.write(html)

        def _body(self) -> dict[str, Any]:
            length = int(self.headers.get("Content-Length", "0"))
            if length > 1_000_000:
                raise BusinessError(
                    code=ErrorCode.FORMAT_CORRUPT,
                    message="request body too large",
                    stage="output",
                    field="body",
                )
            if not length:
                return {}
            raw = self.rfile.read(length)
            try:
                data = json.loads(raw.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise BusinessError(
                    code=ErrorCode.FORMAT_CORRUPT,
                    message=f"invalid JSON request: {exc}",
                    stage="output",
                    field="body",
                ) from exc
            if not isinstance(data, dict):
                raise BusinessError(
                    code=ErrorCode.FORMAT_CORRUPT,
                    message="request body must be a JSON object",
                    stage="output",
                    field="body",
                )
            return data

        def do_GET(self) -> None:
            parsed = urlparse(self.path)
            if parsed.path == "/":
                self._html()
                return
            if parsed.path == "/healthz":
                self._json({"status": "ok"})
                return
            if parsed.path == "/api/state":
                self._json(controller.report())
                return
            if parsed.path == "/api/cache":
                key = parse_qs(parsed.query).get("key", [""])[0]
                cache_path = controller.cache_dir
                cache = ArtifactCache(cache_path)
                result = cache.query(key) if key else None
                if result is None:
                    self._json(
                        {
                            "error": {
                                "code": "E_CACHE_MISS",
                                "message": f"no cache entry for key {key}",
                            }
                        },
                        HTTPStatus.NOT_FOUND,
                    )
                else:
                    self._json(result)
                return
            self._json(
                {"error": {"code": "E_NOT_FOUND", "message": "unknown endpoint"}},
                HTTPStatus.NOT_FOUND,
            )

        def do_POST(self) -> None:
            try:
                parsed = urlparse(self.path)
                if parsed.path not in {"/api/demo", "/api/inject"}:
                    self._json(
                        {
                            "error": {
                                "code": "E_NOT_FOUND",
                                "message": "unknown endpoint",
                            }
                        },
                        HTTPStatus.NOT_FOUND,
                    )
                    return
                body = self._body()
                injections = body.get("injections", [])
                if parsed.path == "/api/inject" and not injections:
                    injections = [
                        "parse.axis_missing@mag",
                        "preprocess.calibration_rollback@gyro",
                        "preprocess.sample_rate_nonpositive@accel",
                        "fusion.gain_out_of_range",
                        "output.residual_missing",
                    ]
                report = controller.refresh(injections=injections)
                self._json(report)
            except BusinessError as exc:
                self._json(
                    {"error": exc.to_dict()},
                    _error_status(exc.code),
                )
            except ValueError as exc:
                self._json(
                    {
                        "error": {
                            "code": "E_FORMAT_CORRUPT",
                            "message": str(exc),
                            "stage": "output",
                        }
                    },
                    HTTPStatus.UNPROCESSABLE_ENTITY,
                )

    return Handler


def serve(
    host: str = "127.0.0.1",
    port: int = 8765,
    *,
    cache_dir: str | Path | None = None,
    artifact_path: str | Path | None = None,
) -> None:
    controller = DemoController(cache_dir=cache_dir, artifact_path=artifact_path)
    server = ThreadingHTTPServer((host, port), _make_handler(controller))
    print(f"FlightFusion dashboard: http://{host}:{port}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()

