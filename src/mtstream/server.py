"""Local-only HTTP workbench and diagnostic API."""

from __future__ import annotations

import json
import sys
import threading
from dataclasses import replace
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from .errors import E_STAGE_BUSY, MTStreamError
from .models import DecodeConfig
from .sample_data import (
    make_sample_case,
    modification_token_for,
    unaligned_source_indices,
)
from .selfcheck import run_self_check
from .session import StreamingSession
from .validation import CaseRegistry


class WorkbenchService:
    def __init__(
        self,
        seed: int = 20261120,
        config: DecodeConfig | None = None,
    ) -> None:
        self.seed = int(seed)
        self._lock = threading.RLock()
        self._recent_errors: list[dict[str, Any]] = []
        self._last_selfcheck: dict[str, Any] | None = None
        self._last_preview: dict[str, Any] | None = None
        self._registry = CaseRegistry()
        self._config = config or DecodeConfig(block_size=4, latency_budget_ms=250)
        self.case = make_sample_case(self.seed)
        self.session = self._new_session(self.case, self._config)

    def _new_session(
        self, case: Any, config: DecodeConfig
    ) -> StreamingSession:
        session = StreamingSession(case, config)
        self._registry.add(case)
        return session

    def reset(self) -> dict[str, Any]:
        with self._lock:
            self._recent_errors.clear()
            self._last_selfcheck = None
            self._last_preview = None
            self.case = make_sample_case(self.seed)
            self._config = DecodeConfig(block_size=4, latency_budget_ms=250)
            self._registry = CaseRegistry()
            self.session = self._new_session(self.case, self._config)
            return self.status()

    def note_error(self, error: MTStreamError) -> None:
        with self._lock:
            self._recent_errors.append(error.to_dict())
            self._recent_errors = self._recent_errors[-20:]
            self.session.record_error(error)

    def status(self) -> dict[str, Any]:
        with self._lock:
            diagnostics = self.session.diagnostics()
            diagnostics["recent_errors"] = list(self._recent_errors)
            diagnostics["last_selfcheck"] = self._last_selfcheck
            diagnostics["last_preview"] = self._last_preview
            diagnostics["service"] = {
                "local_only": True,
                "seed": self.seed,
                "case_registry": list(self._registry.snapshot()),
            }
            return diagnostics

    def request_block(self, index: int | None = None) -> dict[str, Any]:
        with self._lock:
            block = (
                self.session.request_next_block()
                if index is None
                else self.session.request_block(int(index))
            )
            return {"ok": True, "block": block.to_dict()}

    def full_decode(self) -> dict[str, Any]:
        with self._lock:
            full = self.session.full_decode()
            return {"ok": True, "full": full.to_dict()}

    def candidates(self, block_index: int) -> dict[str, Any]:
        with self._lock:
            payload = self.session.query_candidates(int(block_index))
            payload["ok"] = True
            return payload

    def preview(self, indices: list[int] | None = None) -> dict[str, Any]:
        with self._lock:
            if indices is None:
                indices = list(range(len(self.session.plan.boundaries)))
            result = self.session.preview_blocks(indices)
            self._last_preview = {
                "preview_count": result["preview_count"],
                "max_workers": result["max_workers"],
                "all_consistent": all(
                    item["consistent"] for item in result["previews"]
                ),
                "timings_ms": {
                    item["block_index"]: item["observed_ms"]
                    for item in result["previews"]
                },
            }
            return {"ok": True, **result}

    def submit_plan(self) -> dict[str, Any]:
        with self._lock:
            plan = self.session.submit_plan()
            return {"ok": True, "plan": plan.to_dict()}

    def invalidate(self, body: dict[str, Any]) -> dict[str, Any]:
        with self._lock:
            mode = str(body.get("mode", "unaligned"))
            apply_change = bool(body.get("apply", True))
            new_case = self.case
            new_config = self._config
            changed_detail: dict[str, Any] = {"mode": mode}
            if mode == "unaligned":
                candidates = unaligned_source_indices(self.case)
                if not candidates:
                    raise MTStreamError(
                        "E_CONFIG_INVALID",
                        "sample has no unaligned source position",
                        field="source_tokens",
                    )
                position = int(body.get("position", candidates[0]))
                if position not in candidates:
                    raise MTStreamError(
                        "E_CONFIG_INVALID",
                        "selected source position is aligned",
                        field="position",
                        data={"position": position},
                    )
                new_case = self.case.with_source_token(
                    position, modification_token_for(self.case, position)
                )
                changed_detail["position"] = position
            elif mode == "aligned":
                candidates = sorted(self.case.aligned_source_indices())
                position = int(body.get("position", candidates[0]))
                if position not in candidates:
                    raise MTStreamError(
                        "E_CONFIG_INVALID",
                        "selected source position is not aligned",
                        field="position",
                        data={"position": position},
                    )
                new_case = self.case.with_source_token(
                    position, modification_token_for(self.case, position)
                )
                changed_detail["position"] = position
            elif mode == "align_version":
                new_case = self.case.with_align_version(
                    self.case.align_version + 1
                )
                changed_detail["align_version"] = new_case.align_version
            elif mode == "block_size":
                requested = int(body.get("block_size", self._config.block_size + 2))
                new_config = replace(self._config, block_size=requested)
                changed_detail["block_size"] = requested
            else:
                raise MTStreamError(
                    "E_CONFIG_INVALID",
                    "unknown invalidation mode",
                    field="mode",
                    data={"mode": mode},
                )

            report = self.session.analyze_invalidation(new_case, new_config)
            if apply_change:
                report = self.session.apply_case_change(new_case, new_config)
                self.case = new_case
                self._config = new_config
                if new_case.case_id not in self._registry.snapshot():
                    self._registry.add(new_case)
            return {
                "ok": True,
                "applied": apply_change,
                "change": changed_detail,
                "invalidation": report.to_dict(),
                "status": self.status(),
            }

    def selfcheck(self) -> dict[str, Any]:
        with self._lock:
            report = run_self_check(
                self.case,
                self._config,
                raise_on_failure=False,
            )
            self._last_selfcheck = {
                "ok": report["ok"],
                "checks": [
                    {"name": check["name"], "ok": check["ok"]}
                    for check in report["checks"]
                ],
                "full_semantic_hash": report["full_semantic_hash"],
            }
            return {"ok": report["ok"], "selfcheck": report}


class _WorkbenchHandler(BaseHTTPRequestHandler):
    service: WorkbenchService
    server_version = "MTStreamWorkbench/0.1"

    def log_message(self, format: str, *args: Any) -> None:
        return

    def _send_json(
        self, payload: dict[str, Any], status: int = HTTPStatus.OK
    ) -> None:
        data = json.dumps(
            payload, ensure_ascii=False, allow_nan=False
        ).encode("utf-8")
        self.send_response(int(status))
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def _send_html(self, path: Path) -> None:
        data = path.read_bytes()
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def _error_status(self, code: str) -> HTTPStatus:
        mapping = {
            "E_STAGE_BUSY": HTTPStatus.CONFLICT,
            "E_INCONSISTENT": HTTPStatus.CONFLICT,
            "E_CHUNK_MISMATCH": HTTPStatus.CONFLICT,
            "E_SNAPSHOT_ROLLBACK": HTTPStatus.CONFLICT,
            "E_LATENCY_BUDGET": HTTPStatus.REQUEST_TIMEOUT,
            "E_RESOURCE_LIMIT": HTTPStatus.REQUEST_ENTITY_TOO_LARGE,
            "E_CACHE_CORRUPT": HTTPStatus.INTERNAL_SERVER_ERROR,
            "E_TOKEN_UNKNOWN": HTTPStatus.UNPROCESSABLE_ENTITY,
            "E_ALIGN_INVALID": HTTPStatus.UNPROCESSABLE_ENTITY,
            "E_CONFIG_INVALID": HTTPStatus.UNPROCESSABLE_ENTITY,
            "E_DUP_CASE": HTTPStatus.CONFLICT,
        }
        return mapping.get(code, HTTPStatus.BAD_REQUEST)

    def _read_json(self) -> dict[str, Any]:
        length = int(self.headers.get("Content-Length", "0"))
        if length <= 0:
            return {}
        raw = self.rfile.read(length)
        try:
            value = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise MTStreamError(
                "E_CONFIG_INVALID",
                "request body must be valid JSON",
                field="body",
                data={"reason": str(exc)},
            ) from exc
        if not isinstance(value, dict):
            raise MTStreamError(
                "E_CONFIG_INVALID",
                "request body must be a JSON object",
                field="body",
            )
        return value

    def _dispatch(self, method: str) -> None:
        parsed = urlparse(self.path)
        path = parsed.path
        try:
            if method == "GET" and path in ("/", "/index.html"):
                self._send_html(Path(__file__).with_name("web") / "index.html")
                return
            if method == "GET" and path == "/api/status":
                self._send_json(self.service.status())
                return
            if method == "GET" and path == "/api/health":
                self._send_json({"ok": True, "status": "ready"})
                return
            if method == "POST" and path == "/api/reset":
                self._send_json(self.service.reset())
                return
            if method == "POST" and path == "/api/block":
                body = self._read_json()
                index = body.get("index")
                payload = self.service.request_block(
                    None if index is None else int(index)
                )
                self._send_json(payload)
                return
            if method == "POST" and path == "/api/full":
                self._send_json(self.service.full_decode())
                return
            if method == "POST" and path == "/api/preview":
                body = self._read_json()
                indices = body.get("indices")
                self._send_json(self.service.preview(indices))
                return
            if method == "POST" and path == "/api/plan/submit":
                self._send_json(self.service.submit_plan())
                return
            if method == "POST" and path == "/api/invalidate":
                self._send_json(self.service.invalidate(self._read_json()))
                return
            if method == "POST" and path == "/api/selfcheck":
                self._send_json(self.service.selfcheck())
                return
            if method == "GET" and path.startswith("/api/blocks/"):
                parts = path.strip("/").split("/")
                if len(parts) == 4 and parts[3] == "candidates":
                    self._send_json(self.service.candidates(int(parts[2])))
                    return
            self._send_json(
                {
                    "ok": False,
                    "error": {
                        "code": "E_CONFIG_INVALID",
                        "message": "unknown endpoint",
                        "field": "path",
                        "data": {"path": path},
                    },
                },
                HTTPStatus.NOT_FOUND,
            )
        except MTStreamError as exc:
            self.service.note_error(exc)
            self._send_json(exc.envelope(), self._error_status(exc.code))
        except (TypeError, ValueError) as exc:
            wrapped = MTStreamError(
                "E_CONFIG_INVALID",
                "request parameter conversion failed",
                field="request",
                data={"reason": str(exc)},
            )
            self.service.note_error(wrapped)
            self._send_json(wrapped.envelope(), HTTPStatus.UNPROCESSABLE_ENTITY)
        except Exception as exc:  # pragma: no cover - defensive server edge
            wrapped = MTStreamError(
                "E_CONFIG_INVALID",
                "unexpected server failure",
                field="server",
                data={"reason": str(exc)},
            )
            self.service.note_error(wrapped)
            self._send_json(wrapped.envelope(), HTTPStatus.INTERNAL_SERVER_ERROR)

    def do_GET(self) -> None:
        self._dispatch("GET")

    def do_POST(self) -> None:
        self._dispatch("POST")


def create_server(
    host: str = "127.0.0.1",
    port: int = 8765,
    *,
    service: WorkbenchService | None = None,
) -> ThreadingHTTPServer:
    service = service or WorkbenchService()
    handler = type(
        "BoundWorkbenchHandler",
        (_WorkbenchHandler,),
        {"service": service},
    )
    server = ThreadingHTTPServer((host, int(port)), handler)
    server.daemon_threads = True
    return server


def serve(host: str = "127.0.0.1", port: int = 8765) -> None:
    server = create_server(host, port)
    actual_host, actual_port = server.server_address[:2]
    print(
        f"workbench listening on http://{actual_host}:{actual_port}",
        file=sys.stderr,
        flush=True,
    )
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()

