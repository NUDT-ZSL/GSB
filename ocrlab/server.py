"""Dependency-free local HTTP application."""

from __future__ import annotations

import argparse
import json
import mimetypes
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlparse

from .config import PipelineConfig
from .errors import BusinessError, E_NOT_FOUND
from .samples import load_manifest
from .serialization import canonical_bytes
from .service import WorkspaceService


ROOT = Path(__file__).resolve().parent.parent
STATIC_DIR = ROOT / "static"


class ApplicationServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, address: tuple[str, int], service: WorkspaceService) -> None:
        super().__init__(address, RequestHandler)
        self.service = service


class RequestHandler(BaseHTTPRequestHandler):
    server: ApplicationServer
    protocol_version = "HTTP/1.1"

    def log_message(self, format_string: str, *args: Any) -> None:
        return

    def _send_bytes(
        self,
        status: int,
        payload: bytes,
        content_type: str,
        *,
        headers: dict[str, str] | None = None,
    ) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(payload)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        if headers:
            for key, value in headers.items():
                self.send_header(key, value)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(payload)

    def _send_json(self, status: int, value: Any) -> None:
        self._send_bytes(
            status,
            canonical_bytes(value) + b"\n",
            "application/json; charset=utf-8",
        )

    def _read_json(self) -> dict[str, Any]:
        length = int(self.headers.get("Content-Length", "0"))
        if length > 2_000_000:
            raise BusinessError(
                "E_INVALID_REQUEST",
                "request body is too large",
                stage="api",
                field="body",
            )
        payload = self.rfile.read(length) if length else b"{}"
        try:
            value = json.loads(payload.decode("utf-8"))
        except Exception as exc:
            raise BusinessError(
                "E_INVALID_REQUEST",
                "request body must be valid JSON",
                stage="api",
                field="body",
            ) from exc
        if not isinstance(value, dict):
            raise BusinessError(
                "E_INVALID_REQUEST",
                "request body must be a JSON object",
                stage="api",
                field="body",
            )
        return value

    def _error(self, error: BusinessError) -> None:
        status = HTTPStatus.UNPROCESSABLE_ENTITY
        if error.code in {"E_NOT_FOUND"}:
            status = HTTPStatus.NOT_FOUND
        elif error.code in {"E_STAGE_BUSY"}:
            status = HTTPStatus.CONFLICT
        elif error.stage in {"api", "validate", "config", "edit"}:
            status = HTTPStatus.BAD_REQUEST
        self._send_json(status, {"ok": False, "error": error.to_dict()})

    def do_HEAD(self) -> None:
        self.do_GET()

    def do_GET(self) -> None:
        try:
            path = urlparse(self.path).path
            if path == "/api/health":
                self._send_json(HTTPStatus.OK, {"ok": True})
                return
            if path == "/api/state":
                self._send_json(HTTPStatus.OK, {"ok": True, "state": self.server.service.state()})
                return
            if path == "/api/report":
                self._send_json(
                    HTTPStatus.OK,
                    {"ok": True, "report": self.server.service.report()},
                )
                return
            if path == "/api/injection-log":
                self._send_json(
                    HTTPStatus.OK,
                    {
                        "ok": True,
                        "records": self.server.service.injection_log(),
                    },
                )
                return
            if path.startswith("/api/images/"):
                page_id = unquote(path[len("/api/images/") :])
                if page_id.endswith(".png"):
                    page_id = page_id[:-4]
                records = load_manifest(self.server.service.data_dir)
                if page_id not in {record["page_id"] for record in records}:
                    raise BusinessError(
                        E_NOT_FOUND,
                        f"image not found: {page_id}",
                        stage="api",
                        page_id=page_id,
                    )
                image_path = self.server.service.data_dir / "images" / f"{page_id}.png"
                if not image_path.exists():
                    raise BusinessError(
                        E_NOT_FOUND,
                        f"image not found: {page_id}",
                        stage="api",
                        page_id=page_id,
                    )
                self._send_bytes(
                    HTTPStatus.OK,
                    image_path.read_bytes(),
                    "image/png",
                    headers={"Content-Disposition": f'inline; filename="{page_id}.png"'},
                )
                return
            self._serve_static(path)
        except BusinessError as error:
            self._error(error)
        except Exception as exc:
            self._send_json(
                HTTPStatus.INTERNAL_SERVER_ERROR,
                {
                    "ok": False,
                    "error": {
                        "code": "E_INTERNAL",
                        "message": str(exc),
                        "stage": "api",
                    },
                },
            )

    def _serve_static(self, path: str) -> None:
        if path in {"", "/"}:
            relative = "index.html"
        else:
            relative = unquote(path.lstrip("/"))
        target = (STATIC_DIR / relative).resolve()
        if STATIC_DIR.resolve() not in target.parents and target != STATIC_DIR.resolve():
            raise BusinessError(
                E_NOT_FOUND,
                "static file not found",
                stage="api",
                field="path",
            )
        if not target.exists() or not target.is_file():
            raise BusinessError(
                E_NOT_FOUND,
                f"static file not found: {path}",
                stage="api",
                field="path",
            )
        media_type = mimetypes.guess_type(str(target))[0] or "application/octet-stream"
        if media_type.startswith("text/") or media_type in {
            "application/javascript",
            "application/json",
        }:
            media_type += "; charset=utf-8"
        self._send_bytes(HTTPStatus.OK, target.read_bytes(), media_type)

    def do_POST(self) -> None:
        try:
            path = urlparse(self.path).path
            body = self._read_json()
            service = self.server.service
            if path == "/api/run":
                injections = body.get("injections", [])
                if not isinstance(injections, list):
                    raise BusinessError(
                        "E_INVALID_REQUEST",
                        "injections must be a list",
                        stage="api",
                        field="injections",
                    )
                result = service.run(
                    injections,
                    reset=bool(body.get("reset", False)),
                )
                self._send_json(HTTPStatus.OK, {"ok": True, "result": result.summary()})
                return
            if path == "/api/demo/injection":
                result = service.demo_injection()
                self._send_json(HTTPStatus.OK, {"ok": True, "result": result.summary()})
                return
            if path == "/api/demo/downgrade":
                result = service.demo_downgrade()
                self._send_json(HTTPStatus.OK, {"ok": True, "result": result.summary()})
                return
            if path == "/api/demo/recovery":
                recovery = service.demo_recovery()
                self._send_json(HTTPStatus.OK, {"ok": True, "recovery": recovery})
                return
            if path == "/api/reset":
                result = service.reset()
                self._send_json(HTTPStatus.OK, {"ok": True, "result": result.summary()})
                return
            if path == "/api/template-upgrade":
                version = str(body.get("version", "glyph-v2"))
                result = service.upgrade_templates(version)
                self._send_json(HTTPStatus.OK, {"ok": True, "result": result.summary()})
                return
            if path == "/api/config":
                effective = body.get("effective", body)
                if not isinstance(effective, dict):
                    raise BusinessError(
                        "E_INVALID_REQUEST",
                        "effective config must be an object",
                        stage="api",
                        field="effective",
                    )
                config = service.update_config(effective)
                self._send_json(
                    HTTPStatus.OK,
                    {
                        "ok": True,
                        "config": {
                            "version": config.version,
                            "checksum": config.config_checksum,
                            "effective": config.to_dict(),
                        },
                    },
                )
                return
            raise BusinessError(E_NOT_FOUND, f"route not found: {path}", stage="api")
        except BusinessError as error:
            self._error(error)
        except Exception as exc:
            self._send_json(
                HTTPStatus.INTERNAL_SERVER_ERROR,
                {
                    "ok": False,
                    "error": {
                        "code": "E_INTERNAL",
                        "message": str(exc),
                        "stage": "api",
                    },
                },
            )

    def do_PUT(self) -> None:
        try:
            path = urlparse(self.path).path
            if path.startswith("/api/pages/"):
                page_id = unquote(path[len("/api/pages/") :])
                body = self._read_json()
                lines = body.get("lines")
                if not isinstance(lines, list):
                    raise BusinessError(
                        "E_INVALID_REQUEST",
                        "lines must be a list",
                        stage="api",
                        page_id=page_id,
                        field="lines",
                    )
                page = self.server.service.edit_page(page_id, lines)
                self._send_json(HTTPStatus.OK, {"ok": True, "page": page})
                return
            raise BusinessError(E_NOT_FOUND, f"route not found: {path}", stage="api")
        except BusinessError as error:
            self._error(error)
        except Exception as exc:
            self._send_json(
                HTTPStatus.INTERNAL_SERVER_ERROR,
                {
                    "ok": False,
                    "error": {
                        "code": "E_INTERNAL",
                        "message": str(exc),
                        "stage": "api",
                    },
                },
            )


def create_server(
    host: str,
    port: int,
    *,
    data_dir: str | Path = "data",
    state_dir: str | Path = ".ocr_state",
    config: PipelineConfig | None = None,
) -> ApplicationServer:
    service = WorkspaceService(data_dir, state_dir, config=config)
    return ApplicationServer((host, port), service)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run the local OCR data laboratory")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--data-dir", default="data")
    parser.add_argument("--state-dir", default=".ocr_state")
    args = parser.parse_args(argv)
    server = create_server(
        args.host,
        args.port,
        data_dir=args.data_dir,
        state_dir=args.state_dir,
    )
    print(f"OCR Laboratory listening on http://{args.host}:{args.port}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

