from __future__ import annotations

import json
import threading
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

from .engine import Engine
from .errors import BusinessError, E_TURN_NOT_FOUND
from .generator import generate_append_turn, generate_session
from .models import Session


class AppContext:
    def __init__(self, *, seed: int = 42, runtime_dir: str | Path = ".runtime/web") -> None:
        self.seed = seed
        self.runtime_dir = Path(runtime_dir)
        self.base_session: Session = generate_session(seed)
        self.engine = Engine(self.base_session, runtime_dir=self.runtime_dir)
        self.engine.build()
        self._operation_lock = threading.RLock()

    def choose_retract_turn(self) -> str:
        active = self.engine.active_turns()
        active_ids = {turn.turn_id for turn in active}
        preferred = self.base_session.metadata.get("demo", {}).get(
            "retract_turn_id"
        )
        if preferred in active_ids:
            return str(preferred)
        for turn in active:
            if turn.role == "user" and turn.candidates:
                return turn.turn_id
        raise BusinessError(
            E_TURN_NOT_FOUND,
            "there is no active user turn available for retraction",
            node="demo",
        )

    def retract(self, turn_id: str | None = None) -> dict[str, Any]:
        with self._operation_lock:
            target = turn_id or self.choose_retract_turn()
            return self.engine.retract(target)

    def append_demo(self) -> dict[str, Any]:
        with self._operation_lock:
            turn = generate_append_turn(self.engine.session)
            return self.engine.append_turn(turn)

    def batch(self, operations: list[dict[str, Any]]) -> dict[str, Any]:
        with self._operation_lock:
            return self.engine.apply_batch(operations)

    def reset(self) -> None:
        with self._operation_lock:
            self.engine = Engine(
                self.base_session,
                runtime_dir=self.runtime_dir,
            )
            self.engine.build()

    def dashboard(self) -> dict[str, Any]:
        dashboard = self.engine.dashboard()
        dashboard["app_title"] = "SlotLine · 多轮槽位状态诊断"
        dashboard["demo_defaults"] = {
            "retract_turn_id": self.choose_retract_turn(),
            "append_strategy": "deterministic_tail_v1",
        }
        return dashboard


class SlotLineHandler(BaseHTTPRequestHandler):
    server: "SlotLineHTTPServer"

    def log_message(self, format: str, *args: Any) -> None:
        return

    def _send_json(self, status: int, value: dict[str, Any]) -> None:
        data = json.dumps(
            value,
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.send_header(
            "Content-Security-Policy",
            "default-src 'self'; style-src 'unsafe-inline'; script-src 'unsafe-inline'",
        )
        self.end_headers()
        self.wfile.write(data)

    def _send_html(self, path: Path) -> None:
        data = path.read_bytes()
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.send_header(
            "Content-Security-Policy",
            "default-src 'self'; style-src 'unsafe-inline'; script-src 'unsafe-inline'",
        )
        self.end_headers()
        self.wfile.write(data)

    def _read_json(self) -> dict[str, Any]:
        length = int(self.headers.get("Content-Length", "0"))
        if length <= 0:
            return {}
        if length > 1_000_000:
            raise BusinessError(
                "E_INVALID_REQUEST",
                "request body is too large",
                node="http",
            )
        raw = self.rfile.read(length)
        try:
            value = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise BusinessError(
                "E_INVALID_REQUEST",
                "request body must be valid JSON",
                node="http",
            ) from exc
        if not isinstance(value, dict):
            raise BusinessError(
                "E_INVALID_REQUEST",
                "request body must be a JSON object",
                node="http",
            )
        return value

    def _error_response(self, exc: BusinessError) -> None:
        try:
            dashboard = self.server.context.dashboard()
        except Exception:
            dashboard = None
        self._send_json(
            HTTPStatus.CONFLICT,
            {
                "ok": False,
                "error": exc.to_dict(),
                "dashboard": dashboard,
            },
        )

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        if parsed.path in {"/", "/index.html"}:
            self._send_html(Path(__file__).parent / "web" / "index.html")
            return
        if parsed.path == "/api/state":
            self._send_json(
                HTTPStatus.OK,
                {"ok": True, "dashboard": self.server.context.dashboard()},
            )
            return
        if parsed.path == "/api/trace":
            slot = parse_qs(parsed.query).get("slot", [""])[0]
            try:
                trace = self.server.context.engine.trace_slot(slot)
                self._send_json(
                    HTTPStatus.OK,
                    {"ok": True, "trace": trace},
                )
            except BusinessError as exc:
                self._error_response(exc)
            return
        if parsed.path == "/api/cache":
            key = parse_qs(parsed.query).get("key", [""])[0]
            try:
                entry = self.server.context.engine.cache.get_entry(key)
                self._send_json(
                    HTTPStatus.OK,
                    {
                        "ok": True,
                        "verified": True,
                        "key": key,
                        "entry": entry,
                    },
                )
            except BusinessError as exc:
                self._error_response(exc)
            except Exception:
                self._send_json(
                    HTTPStatus.CONFLICT,
                    {
                        "ok": False,
                        "error": {
                            "code": "E_CACHE_CORRUPT",
                            "message": "cache key cannot be reproduced",
                            "details": {"key": key},
                        },
                    },
                )
            return
        self._send_json(
            HTTPStatus.NOT_FOUND,
            {"ok": False, "error": {"code": "E_NOT_FOUND"}},
        )

    def do_POST(self) -> None:
        parsed = urlparse(self.path)
        try:
            body = self._read_json()
            if parsed.path == "/api/retract":
                operation = self.server.context.retract(
                    str(body["turn_id"]) if body.get("turn_id") else None
                )
            elif parsed.path == "/api/append":
                operation = self.server.context.append_demo()
            elif parsed.path == "/api/batch":
                operations = body.get("operations")
                if not isinstance(operations, list):
                    raise BusinessError(
                        "E_OPERATION_ORDER",
                        "operations must be a list",
                        node="batch",
                    )
                operation = self.server.context.batch(operations)
            elif parsed.path == "/api/reset":
                self.server.context.reset()
                self._send_json(
                    HTTPStatus.OK,
                    {
                        "ok": True,
                        "dashboard": self.server.context.dashboard(),
                    },
                )
                return
            else:
                self._send_json(
                    HTTPStatus.NOT_FOUND,
                    {"ok": False, "error": {"code": "E_NOT_FOUND"}},
                )
                return
            self._send_json(
                HTTPStatus.OK,
                {
                    "ok": True,
                    "operation": operation,
                    "dashboard": self.server.context.dashboard(),
                },
            )
        except BusinessError as exc:
            self._error_response(exc)
        except Exception as exc:
            self._send_json(
                HTTPStatus.INTERNAL_SERVER_ERROR,
                {
                    "ok": False,
                    "error": {
                        "code": "E_INTERNAL",
                        "message": str(exc),
                    },
                },
            )


class SlotLineHTTPServer(ThreadingHTTPServer):
    context: AppContext

    def __init__(
        self,
        server_address: tuple[str, int],
        context: AppContext,
    ) -> None:
        self.context = context
        super().__init__(server_address, SlotLineHandler)


def create_server(
    *,
    host: str = "127.0.0.1",
    port: int = 8765,
    seed: int = 42,
    runtime_dir: str | Path = ".runtime/web",
) -> SlotLineHTTPServer:
    return SlotLineHTTPServer(
        (host, port),
        AppContext(seed=seed, runtime_dir=runtime_dir),
    )
