"""Local HTTP application for the interactive LockFair dashboard."""

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

from . import __version__
from .engine import LockFairEngine
from .errors import BusinessError, ErrorCode, error
from .models import load_bundle


WEB_ROOT = Path(__file__).resolve().parent.parent / "web"
MAX_BODY_BYTES = 1024 * 1024


class DashboardServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(
        self,
        server_address: tuple[str, int],
        *,
        db_path: Path,
        bundle_path: Path,
    ):
        super().__init__(server_address, DashboardHandler)
        self.db_path = db_path
        self.bundle_path = bundle_path
        self.engine = LockFairEngine(db_path)


class DashboardHandler(BaseHTTPRequestHandler):
    server: DashboardServer
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt: str, *args: Any) -> None:
        print(f"[lockfair] {fmt % args}")

    def _send_json(self, payload: Any, status: int = 200) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _send_file(self, path: Path) -> None:
        if not path.exists() or not path.is_file():
            self.send_error(HTTPStatus.NOT_FOUND)
            return
        body = path.read_bytes()
        content_type = mimetypes.guess_type(str(path))[0] or "application/octet-stream"
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        self.wfile.write(body)

    def _read_body(self) -> dict[str, Any]:
        length = int(self.headers.get("Content-Length", "0"))
        if length > MAX_BODY_BYTES:
            raise error(ErrorCode.INVALID_INPUT, "请求体过大")
        if length == 0:
            return {}
        raw = self.rfile.read(length)
        try:
            payload = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise error(ErrorCode.INVALID_INPUT, f"请求 JSON 无效: {exc}") from exc
        if not isinstance(payload, dict):
            raise error(ErrorCode.INVALID_INPUT, "请求体必须是 JSON 对象")
        return payload

    def _current_state(self, round_no: int | None = None) -> dict[str, Any]:
        snapshot = self.server.engine.get_state(round_no=round_no)
        state = snapshot["state"]
        rounds = self.server.engine.list_rounds()
        errors = self.server.engine.list_errors(limit=200)
        events = self.server.engine.list_queue_events(round_no=snapshot["round_no"], limit=300)
        recovery = self.server.engine.list_recovery_log(limit=50)
        return {
            "ok": True,
            "app_version": __version__,
            "db_path": str(self.server.db_path),
            "bundle_path": str(self.server.bundle_path),
            "snapshot": snapshot,
            "state": state,
            "rounds": rounds,
            "errors": errors,
            "events": events,
            "recovery_log": recovery,
        }

    def _handle_business_error(self, exc: BusinessError) -> None:
        status = 409 if exc.code in {
            ErrorCode.VERSION_CONFLICT,
            ErrorCode.ROUND_OUT_OF_ORDER,
            ErrorCode.STATE_ALREADY_INITIALIZED,
        } else 400
        self._send_json(exc.to_dict(), status=status)

    def do_GET(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        try:
            if parsed.path == "/" or parsed.path == "/index.html":
                self._send_file(WEB_ROOT / "index.html")
                return
            if parsed.path.startswith("/assets/"):
                relative = parsed.path[len("/assets/") :]
                candidate = (WEB_ROOT / relative).resolve()
                if WEB_ROOT.resolve() not in candidate.parents and candidate != WEB_ROOT.resolve():
                    self.send_error(HTTPStatus.FORBIDDEN)
                    return
                self._send_file(candidate)
                return
            if parsed.path == "/api/health":
                snapshot = self.server.engine.get_state()
                self._send_json(
                    {
                        "ok": True,
                        "app": "LockFair",
                        "version": __version__,
                        "round_no": snapshot["round_no"],
                        "state_version": snapshot["version"],
                    }
                )
                return
            if parsed.path in {"/api/state", "/api/snapshot"}:
                query = parse_qs(parsed.query)
                round_no = int(query["round"][0]) if query.get("round") else None
                self._send_json(self._current_state(round_no))
                return
            if parsed.path == "/api/rounds":
                self._send_json({"ok": True, "rounds": self.server.engine.list_rounds()})
                return
            if parsed.path == "/api/errors":
                self._send_json({"ok": True, "errors": self.server.engine.list_errors(500)})
                return
            if parsed.path == "/api/events":
                query = parse_qs(parsed.query)
                round_no = int(query["round"][0]) if query.get("round") else None
                self._send_json(
                    {
                        "ok": True,
                        "events": self.server.engine.list_queue_events(round_no=round_no, limit=500),
                    }
                )
                return
            if parsed.path == "/api/config":
                bundle = load_bundle(self.server.bundle_path)
                self._send_json({"ok": True, "bundle": bundle.to_dict(), "hash": bundle.canonical_hash()})
                return
            self.send_error(HTTPStatus.NOT_FOUND)
        except BusinessError as exc:
            self._handle_business_error(exc)
        except Exception as exc:  # pragma: no cover - defensive HTTP boundary
            self._send_json(
                {
                    "ok": False,
                    "error": {
                        "code": "INTERNAL_ERROR",
                        "message": str(exc),
                        "round_no": None,
                        "lock_key": None,
                        "application_seq": None,
                        "constraint": None,
                        "details": {"type": type(exc).__name__},
                    },
                },
                status=500,
            )

    def do_POST(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        try:
            body = self._read_body()
            if parsed.path == "/api/step":
                snapshot = self.server.engine.execute_round(
                    round_no=body.get("round_no"),
                    clock_ms=body.get("clock_ms"),
                    request_id=body.get("request_id"),
                    expected_version=body.get("expected_version"),
                )
                self._send_json({"ok": True, "snapshot": snapshot, "dashboard": self._current_state()})
                return
            if parsed.path == "/api/run":
                count = body.get("count", 1)
                if not isinstance(count, int) or isinstance(count, bool) or count <= 0 or count > 50:
                    raise error(ErrorCode.INVALID_INPUT, "count 必须是 1..50 的整数")
                results = self.server.engine.run_rounds(count)
                self._send_json(
                    {
                        "ok": True,
                        "rounds": [
                            {
                                "round_no": item["round_no"],
                                "state_version": item["version"],
                                "summary": item.get("summary"),
                            }
                            for item in results
                        ],
                        "dashboard": self._current_state(),
                    }
                )
                return
            if parsed.path == "/api/reset":
                bundle_path = Path(body.get("bundle_path", self.server.bundle_path))
                bundle = load_bundle(bundle_path)
                if bundle_path.resolve() != self.server.bundle_path.resolve():
                    self.server.bundle_path = bundle_path.resolve()
                self.server.engine.initialize(bundle, force=True)
                self._send_json({"ok": True, "dashboard": self._current_state(), "bundle": bundle.to_dict()})
                return
            if parsed.path == "/api/recover":
                result = self.server.engine.recover()
                self._send_json({"ok": True, "recovery": result, "dashboard": self._current_state()})
                return
            if parsed.path == "/api/submit":
                result = self.server.engine.submit_application(
                    body.get("application", {}),
                    expected_version=body.get("expected_version"),
                )
                self._send_json({"ok": True, "result": result, "dashboard": self._current_state()})
                return
            if parsed.path == "/api/cancel":
                seq = body.get("seq")
                if not isinstance(seq, int) or isinstance(seq, bool):
                    raise error(ErrorCode.INVALID_INPUT, "seq 必须是整数")
                result = self.server.engine.cancel_application(
                    seq,
                    mode="abandoned" if body.get("abandon") else "cancelled",
                    reason=str(body.get("reason", "operator_cancelled")),
                )
                self._send_json({"ok": True, "result": result, "dashboard": self._current_state()})
                return
            self.send_error(HTTPStatus.NOT_FOUND)
        except BusinessError as exc:
            self._handle_business_error(exc)
        except Exception as exc:  # pragma: no cover - defensive HTTP boundary
            self._send_json(
                {
                    "ok": False,
                    "error": {
                        "code": "INTERNAL_ERROR",
                        "message": str(exc),
                        "round_no": None,
                        "lock_key": None,
                        "application_seq": None,
                        "constraint": None,
                        "details": {"type": type(exc).__name__},
                    },
                },
                status=500,
            )


def run_server(
    *,
    db_path: Path,
    bundle_path: Path,
    host: str = "127.0.0.1",
    port: int = 8765,
    open_browser: bool = False,
) -> None:
    server = DashboardServer(
        (host, port),
        db_path=db_path.resolve(),
        bundle_path=bundle_path.resolve(),
    )
    url = f"http://{host}:{port}/"
    print(f"LockFair dashboard: {url}")
    print(f"SQLite state: {db_path.resolve()}")
    print("Press Ctrl+C to stop.")
    if open_browser:
        threading.Timer(0.4, lambda: webbrowser.open(url)).start()
    try:
        server.serve_forever(poll_interval=0.25)
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
