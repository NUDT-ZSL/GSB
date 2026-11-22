from __future__ import annotations

import json
import time
import traceback
from collections import deque
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from threading import RLock
from typing import Any
from urllib.parse import parse_qs, urlsplit

from .errors import WorkbenchError
from .service import DedupService

WEB_ROOT = Path(__file__).resolve().parent / "webui"
STATIC_FILES = {
    "/": ("index.html", "text/html; charset=utf-8"),
    "/index.html": ("index.html", "text/html; charset=utf-8"),
    "/static/app.js": ("app.js", "text/javascript; charset=utf-8"),
    "/static/style.css": ("style.css", "text/css; charset=utf-8"),
}


class Diagnostics:
    def __init__(self) -> None:
        self._lock = RLock()
        self.latencies: deque[dict[str, Any]] = deque(maxlen=100)
        self.errors: deque[dict[str, Any]] = deque(maxlen=40)
        self.transitions: deque[dict[str, Any]] = deque(maxlen=10)

    def record_query(self, result: dict[str, Any]) -> None:
        with self._lock:
            latency = result["latency"]
            self.latencies.appendleft(
                {
                    "doc_id": result["doc_id"],
                    "actual_ms": latency["actual_ms"],
                    "estimated_ms": latency["estimated_ms"],
                    "budget_ms": latency["budget_ms"],
                    "stages": latency["stages"],
                    "removed": result["removed"],
                    "ok": True,
                }
            )

    def record_error(self, error: WorkbenchError) -> None:
        with self._lock:
            self.errors.appendleft(
                {
                    "at": time.strftime("%Y-%m-%dT%H:%M:%S"),
                    "code": error.code,
                    "message": error.message,
                    "stage": error.stage,
                    "completed_stages": error.completed_stages,
                    "details": error.details,
                }
            )

    def record_transition(self, result: dict[str, Any]) -> None:
        with self._lock:
            if result.get("transition"):
                self.transitions.appendleft(result["transition"])

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            return {
                "latencies": list(self.latencies),
                "errors": list(self.errors),
                "transitions": list(self.transitions),
            }


class WorkbenchHTTPServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True
    request_queue_size = 32

    def __init__(
        self,
        server_address: tuple[str, int],
        service: DedupService,
    ) -> None:
        self.service = service
        self.diagnostics = Diagnostics()
        super().__init__(server_address, WorkbenchHandler)


class WorkbenchHandler(BaseHTTPRequestHandler):
    server: WorkbenchHTTPServer

    def log_message(self, format: str, *args: Any) -> None:
        return

    def _send_bytes(
        self,
        body: bytes,
        content_type: str,
        status: int = HTTPStatus.OK,
    ) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(body)

    def _send_json(self, value: Any, status: int = HTTPStatus.OK) -> None:
        body = json.dumps(value, ensure_ascii=False).encode("utf-8")
        self._send_bytes(body, "application/json; charset=utf-8", status)

    def _read_json(self) -> dict[str, Any]:
        length = int(self.headers.get("Content-Length", "0"))
        if length <= 0:
            return {}
        if length > 2_000_000:
            raise WorkbenchError(
                "E_CONFIG_INVALID",
                "请求体过大",
                stage="http_request",
            )
        raw = self.rfile.read(length)
        try:
            value = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise WorkbenchError(
                "E_CONFIG_INVALID",
                "请求 JSON 无效",
                stage="http_request",
                details={"reason": str(exc)},
            ) from exc
        if not isinstance(value, dict):
            raise WorkbenchError(
                "E_CONFIG_INVALID",
                "请求 JSON 根节点必须为对象",
                stage="http_request",
            )
        return value

    def do_GET(self) -> None:
        parsed = urlsplit(self.path)
        if parsed.path in STATIC_FILES:
            filename, content_type = STATIC_FILES[parsed.path]
            path = WEB_ROOT / filename
            self._send_bytes(path.read_bytes(), content_type)
            return
        try:
            if parsed.path == "/api/status":
                payload = self.server.service.status()
                payload["diagnostics"] = self.server.diagnostics.snapshot()
                self._send_json(payload)
            elif parsed.path == "/api/dashboard":
                payload = self.server.service.dashboard()
                payload["diagnostics"] = self.server.diagnostics.snapshot()
                self._send_json(payload)
            elif parsed.path == "/api/query":
                query = parse_qs(parsed.query)
                doc_id = (query.get("doc_id") or [""])[0]
                if not doc_id:
                    raise WorkbenchError(
                        "E_DOC_NOT_FOUND",
                        "缺少 doc_id 参数",
                        stage="query_lookup",
                    )
                budget_values = query.get("budget_ms")
                try:
                    budget = float(budget_values[0]) if budget_values else None
                except ValueError as exc:
                    raise WorkbenchError(
                        "E_LATENCY_BUDGET",
                        "budget_ms 必须为数值",
                        stage="query_budget",
                    ) from exc
                result = self.server.service.query(doc_id, budget)
                self.server.diagnostics.record_query(result)
                self._send_json(result)
            elif parsed.path == "/api/compare-modes":
                self._send_json(self.server.service.compare_modes())
            elif parsed.path == "/api/consistency":
                self._send_json(self.server.service.validate_all_consistency())
            elif parsed.path == "/api/snapshot":
                kind = (parse_qs(parsed.query).get("kind") or [""])[0]
                if not kind:
                    raise WorkbenchError(
                        "E_SNAPSHOT_CORRUPT",
                        "缺少快照 kind 参数",
                        stage="snapshot_read",
                    )
                self._send_json(self.server.service.get_snapshot(kind))
            elif parsed.path == "/api/health":
                self._send_json({"ok": True})
            else:
                self._send_json(
                    {
                        "ok": False,
                        "error": {
                            "code": "E_NOT_FOUND",
                            "message": "未知 API 路径",
                            "stage": "http_route",
                        },
                    },
                    HTTPStatus.NOT_FOUND,
                )
        except WorkbenchError as exc:
            self.server.diagnostics.record_error(exc)
            self._send_json(exc.to_dict())
        except Exception as exc:  # noqa: BLE001  # pragma: no cover
            traceback.print_exc()
            self._send_json(
                {
                    "ok": False,
                    "error": {
                        "code": "E_INTERNAL",
                        "message": str(exc),
                        "stage": "http_handler",
                    },
                },
                HTTPStatus.INTERNAL_SERVER_ERROR,
            )

    def do_POST(self) -> None:
        parsed = urlsplit(self.path)
        try:
            if parsed.path == "/api/config":
                updates = self._read_json()
                result = self.server.service.reconfigure(updates)
                self.server.diagnostics.record_transition(result)
                self._send_json(result)
            elif parsed.path == "/api/refresh":
                result = self.server.service.refresh()
                self._send_json(
                    {
                        "ok": True,
                        "cache_report": result["cache_report"],
                        "snapshots": result["snapshots"],
                    }
                )
            else:
                self._send_json(
                    {
                        "ok": False,
                        "error": {
                            "code": "E_NOT_FOUND",
                            "message": "未知 POST API 路径",
                            "stage": "http_route",
                        },
                    },
                    HTTPStatus.NOT_FOUND,
                )
        except WorkbenchError as exc:
            self.server.diagnostics.record_error(exc)
            self._send_json(exc.to_dict())
        except Exception as exc:  # noqa: BLE001  # pragma: no cover
            traceback.print_exc()
            self._send_json(
                {
                    "ok": False,
                    "error": {
                        "code": "E_INTERNAL",
                        "message": str(exc),
                        "stage": "http_handler",
                    },
                },
                HTTPStatus.INTERNAL_SERVER_ERROR,
            )


def run_server(
    service: DedupService,
    host: str = "127.0.0.1",
    port: int = 8765,
) -> None:
    server = WorkbenchHTTPServer((host, port), service)
    url = f"http://{host}:{server.server_port}"
    print(f"语料去重诊断工作台已启动: {url}", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()

