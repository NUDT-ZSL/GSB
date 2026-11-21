from __future__ import annotations

import json
import threading
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from .demo import run_demo, write_demo_outputs
from .errors import AppError
from .generation import load_documents, load_queries, write_samples
from .util import canonical_json


class DemoState:
    def __init__(self, workspace: Path) -> None:
        self.workspace = workspace
        self.lock = threading.Lock()
        self.last_result: dict[str, Any] | None = None

    def run(self, conv_id: str | None = None, turn_no: int | None = None) -> dict[str, Any]:
        if not self.lock.acquire(blocking=False):
            raise AppError(
                "E_STAGE_BUSY",
                "another demo execution is already active",
                {"resource": "demo"},
            )
        try:
            kwargs: dict[str, Any] = {"workspace": self.workspace}
            if conv_id is not None:
                kwargs["primary_conv"] = conv_id
            if turn_no is not None:
                kwargs["primary_turn"] = turn_no
            result = run_demo(**kwargs)
            result["outputs"] = write_demo_outputs(self.workspace, result)
            self.last_result = result
            return result
        finally:
            self.lock.release()


def make_handler(state: DemoState) -> type[BaseHTTPRequestHandler]:
    web_root = Path(__file__).resolve().parent / "web"

    class Handler(BaseHTTPRequestHandler):
        server_version = "CQIR/1.0"

        def log_message(self, format: str, *args: Any) -> None:
            return

        def _json(self, status: int, value: Any) -> None:
            body = canonical_json(value).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def _error(self, exc: AppError, status: int = HTTPStatus.UNPROCESSABLE_ENTITY) -> None:
            self._json(status, exc.to_dict())

        def _read_json(self) -> dict[str, Any]:
            length = int(self.headers.get("Content-Length", "0"))
            if length <= 0:
                return {}
            raw = self.rfile.read(length).decode("utf-8")
            try:
                value = json.loads(raw)
            except json.JSONDecodeError as exc:
                raise AppError(
                    "E_INPUT",
                    "request JSON cannot be parsed",
                    {"line": exc.lineno, "column": exc.colno},
                ) from exc
            if not isinstance(value, dict):
                raise AppError("E_INPUT", "request JSON must be an object")
            return value

        def do_GET(self) -> None:
            path = urlparse(self.path).path
            try:
                if path in ("/", "/index.html"):
                    body = (web_root / "index.html").read_bytes()
                    self.send_response(HTTPStatus.OK)
                    self.send_header("Content-Type", "text/html; charset=utf-8")
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)
                    return
                if path == "/api/health":
                    self._json(
                        HTTPStatus.OK,
                        {"ok": True, "service": "cqir", "has_result": state.last_result is not None},
                    )
                    return
                if path == "/api/bootstrap":
                    samples = write_samples(state.workspace / "samples")
                    queries = load_queries(state.workspace / "samples" / "queries.jsonl")
                    documents = load_documents(state.workspace / "samples" / "documents.json")
                    self._json(
                        HTTPStatus.OK,
                        {
                            "ok": True,
                            "samples": samples,
                            "queries": [query.to_dict() for query in queries],
                            "documents": [
                                {
                                    "doc_id": document.doc_id,
                                    "category": document.category,
                                    "doc_version": document.doc_version,
                                    "codepoint_len": len(document.text),
                                    "term_count": len(document.terms),
                                }
                                for document in documents.values()
                            ],
                            "has_result": state.last_result is not None,
                        },
                    )
                    return
                if path == "/api/last":
                    if state.last_result is None:
                        self._json(HTTPStatus.NOT_FOUND, {"ok": False, "error": {"code": "E_NOT_READY"}})
                    else:
                        self._json(HTTPStatus.OK, state.last_result)
                    return
                self._json(
                    HTTPStatus.NOT_FOUND,
                    {"ok": False, "error": {"code": "E_NOT_FOUND", "path": path}},
                )
            except AppError as exc:
                self._error(exc)
            except Exception as exc:
                self._json(
                    HTTPStatus.INTERNAL_SERVER_ERROR,
                    {"ok": False, "error": {"code": "E_INTERNAL", "message": str(exc)}},
                )

        def do_POST(self) -> None:
            path = urlparse(self.path).path
            try:
                body = self._read_json()
                if path == "/api/demo":
                    conv_id = body.get("conv_id")
                    turn_no = body.get("turn_no")
                    if turn_no is not None:
                        try:
                            turn_no = int(turn_no)
                        except (TypeError, ValueError) as exc:
                            raise AppError("E_INPUT", "turn_no must be an integer") from exc
                    result = state.run(
                        str(conv_id) if conv_id is not None else None,
                        turn_no,
                    )
                    self._json(HTTPStatus.OK, result)
                    return
                self._json(
                    HTTPStatus.NOT_FOUND,
                    {"ok": False, "error": {"code": "E_NOT_FOUND", "path": path}},
                )
            except AppError as exc:
                status = HTTPStatus.CONFLICT if exc.code == "E_STAGE_BUSY" else HTTPStatus.UNPROCESSABLE_ENTITY
                self._error(exc, status)
            except Exception as exc:
                self._json(
                    HTTPStatus.INTERNAL_SERVER_ERROR,
                    {"ok": False, "error": {"code": "E_INTERNAL", "message": str(exc)}},
                )

    return Handler


def serve(workspace: Path, host: str = "127.0.0.1", port: int = 8765) -> None:
    state = DemoState(workspace)
    handler = make_handler(state)
    server = ThreadingHTTPServer((host, port), handler)
    print(f"CQIR local console: http://{host}:{port}")
    print("Press Ctrl+C to stop.")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
