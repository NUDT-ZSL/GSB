"""Dependency-free local HTTP server and JSON API."""

from __future__ import annotations

import json
import mimetypes
import threading
import webbrowser
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

from .errors import AppError
from .service import LabService

STATIC_ROOT = Path(__file__).resolve().parent / "static"


class LabRequestHandler(BaseHTTPRequestHandler):
    service: LabService
    server_version = "VocalForgeLocal/1.0"

    def log_message(self, format: str, *args: Any) -> None:
        print(f"[web] {self.address_string()} - {format % args}")

    def _send_json(self, payload: Any, status: int = HTTPStatus.OK) -> None:
        body = json.dumps(payload, ensure_ascii=False, allow_nan=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _read_json(self) -> dict[str, Any]:
        length = int(self.headers.get("Content-Length", "0"))
        if length <= 0:
            return {}
        raw = self.rfile.read(length)
        try:
            value = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise AppError(
                "E_INPUT_INVALID",
                "Request body is not valid JSON.",
                stage="web.json",
                details={"error": str(exc)},
            ) from exc
        if not isinstance(value, dict):
            raise AppError(
                "E_INPUT_INVALID",
                "Request body must be a JSON object.",
                stage="web.json",
            )
        return value

    def _serve_static(self, path: str) -> None:
        name = "index.html" if path in ("", "/") else path.lstrip("/")
        target = (STATIC_ROOT / name).resolve()
        if STATIC_ROOT not in target.parents and target != STATIC_ROOT:
            self.send_error(HTTPStatus.NOT_FOUND)
            return
        if not target.is_file():
            self.send_error(HTTPStatus.NOT_FOUND)
            return
        data = target.read_bytes()
        content_type = mimetypes.guess_type(str(target))[0] or "application/octet-stream"
        if target.suffix == ".js":
            content_type = "text/javascript; charset=utf-8"
        elif target.suffix == ".css":
            content_type = "text/css; charset=utf-8"
        elif target.suffix == ".html":
            content_type = "text/html; charset=utf-8"
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        if not parsed.path.startswith("/api/"):
            self._serve_static(parsed.path)
            return
        try:
            query = parse_qs(parsed.query)
            if parsed.path == "/api/health":
                self._send_json(self.service.health())
            elif parsed.path == "/api/state":
                self._send_json(self.service.state())
            elif parsed.path == "/api/alignment":
                profile = query.get("profile", [None])[0]
                utterance_id = query.get("utterance_id", [None])[0]
                self._send_json(
                    self.service.alignment_payload(profile, utterance_id)
                )
            elif parsed.path == "/api/precision":
                self._send_json(self.service.precision_payload())
            elif parsed.path == "/api/error-spectrum":
                profile = query.get("profile", [None])[0]
                self._send_json(self.service.error_spectrum(profile))
            elif parsed.path == "/api/similarity":
                profile = query.get("profile", [None])[0]
                self._send_json(self.service.similarity_payload(profile))
            elif parsed.path == "/api/snapshot":
                self._send_json(self.service.snapshot_state())
            elif parsed.path == "/api/consistency":
                self._send_json(self.service.run_consistency())
            else:
                self._send_json(
                    {"ok": False, "error": {"code": "E_NOT_FOUND"}},
                    status=HTTPStatus.NOT_FOUND,
                )
        except AppError as error:
            record = self.service.record_error(error, entrypoint=f"GET {parsed.path}")
            self._send_json(
                {
                    "ok": False,
                    "error": error.to_dict(),
                    "chain": [record],
                    "degradation": record["degradation"],
                }
            )
        except Exception as exc:  # pragma: no cover - boundary guard
            self._send_json(
                {
                    "ok": False,
                    "error": {
                        "code": "E_INTERNAL",
                        "message": str(exc),
                        "stage": "web.unhandled",
                    },
                },
                status=HTTPStatus.INTERNAL_SERVER_ERROR,
            )

    def do_POST(self) -> None:
        parsed = urlparse(self.path)
        try:
            body = self._read_json()
            if parsed.path == "/api/registration":
                payload = self.service.append_registration(
                    speaker_id=int(body.get("speaker_id", 1)),
                    ref_id=body.get("ref_id"),
                )
                self._send_json(payload)
            elif parsed.path == "/api/precision":
                profile = str(body.get("profile", ""))
                self._send_json(self.service.switch_profile(profile))
            elif parsed.path == "/api/similarity-config":
                self._send_json(
                    self.service.update_similarity_config(
                        threshold=body.get("threshold"),
                        top_k=body.get("top_k"),
                        strategy=body.get("strategy"),
                        tolerance=body.get("tolerance"),
                    )
                )
            elif parsed.path == "/api/inject":
                self._send_json(
                    self.service.inject_error(str(body.get("stage", "")))
                )
            elif parsed.path == "/api/rollback":
                self._send_json(
                    self.service.attempt_snapshot_rollback(
                        int(body.get("target_version", 0))
                    )
                )
            else:
                self._send_json(
                    {"ok": False, "error": {"code": "E_NOT_FOUND"}},
                    status=HTTPStatus.NOT_FOUND,
                )
        except AppError as error:
            record = self.service.record_error(error, entrypoint=f"POST {parsed.path}")
            self._send_json(
                {
                    "ok": False,
                    "error": error.to_dict(),
                    "chain": [record],
                    "degradation": record["degradation"],
                }
            )
        except (TypeError, ValueError) as exc:
            self._send_json(
                {
                    "ok": False,
                    "error": {
                        "code": "E_INPUT_INVALID",
                        "message": str(exc),
                        "stage": "web.input",
                    },
                }
            )
        except Exception as exc:  # pragma: no cover - boundary guard
            self._send_json(
                {
                    "ok": False,
                    "error": {
                        "code": "E_INTERNAL",
                        "message": str(exc),
                        "stage": "web.unhandled",
                    },
                },
                status=HTTPStatus.INTERNAL_SERVER_ERROR,
            )


def create_server(
    service: LabService,
    host: str = "127.0.0.1",
    port: int = 8765,
) -> ThreadingHTTPServer:
    handler = type(
        "BoundLabRequestHandler",
        (LabRequestHandler,),
        {"service": service},
    )
    return ThreadingHTTPServer((host, port), handler)


def serve(
    service: LabService,
    host: str = "127.0.0.1",
    port: int = 8765,
    open_browser: bool = True,
) -> None:
    server = create_server(service, host=host, port=port)
    url = f"http://{host}:{port}/"
    print(f"VocalForge Local is running at {url}")
    print("Press Ctrl+C to stop.")
    if open_browser:
        threading.Timer(0.6, lambda: webbrowser.open(url)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopping VocalForge Local.")
    finally:
        server.server_close()
