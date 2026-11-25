"""Deterministic loopback Flask service used by forwarding replay."""

from __future__ import annotations

import threading
import time
import logging
from typing import Any

from flask import Flask, jsonify, request
from werkzeug.serving import make_server


class CallRegistry:
    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.calls: list[dict[str, Any]] = []
        self.inflight: dict[str, int] = {}

    def begin(self, connection_id: str, hop: int) -> str:
        key = f"{connection_id}:{hop}"
        with self.lock:
            self.inflight[key] = self.inflight.get(key, 0) + 1
        return key

    def end(self, key: str, call: dict[str, Any]) -> None:
        with self.lock:
            self.calls.append(call)
            count = self.inflight.get(key, 1) - 1
            if count <= 0:
                self.inflight.pop(key, None)
            else:
                self.inflight[key] = count

    def state(self) -> dict[str, Any]:
        with self.lock:
            counts: dict[str, int] = {}
            for call in self.calls:
                key = f"{call['connection_id']}:{call['hop']}"
                counts[key] = counts.get(key, 0) + 1
            return {
                "call_count": len(self.calls),
                "counts": dict(sorted(counts.items())),
                "inflight": dict(sorted(self.inflight.items())),
                "calls": list(self.calls),
            }


def create_app(plan: dict[str, Any], registry: CallRegistry | None = None) -> Flask:
    app = Flask(__name__)
    calls = plan.get("calls", {})
    registry = registry or CallRegistry()
    app.config["CALL_REGISTRY"] = registry

    @app.get("/__admin/state")
    def admin_state():
        return jsonify(registry.state())

    @app.post("/__admin/reset")
    def admin_reset():
        with registry.lock:
            registry.calls.clear()
            registry.inflight.clear()
        return jsonify({"ok": True})

    @app.route("/mock/<connection_id>/<int:hop>", methods=["GET", "POST"])
    def mock_forward(connection_id: str, hop: int):
        key = registry.begin(connection_id, hop)
        spec = calls.get(connection_id)
        hop_spec = ((spec or {}).get("hops") or {}).get(str(hop))
        try:
            if hop_spec is None:
                response = {
                    "ok": False,
                    "connection_id": connection_id,
                    "hop": hop,
                    "status_code": 404,
                    "digest": None,
                    "message": "no deterministic route/hop plan",
                }
                status = 404
                delay_ms = 0
            else:
                status = int(hop_spec["status"])
                response = {
                    "ok": 200 <= status < 400,
                    "connection_id": connection_id,
                    "hop": hop,
                    "status_code": status,
                    "digest": hop_spec["digest"],
                    "checksum": hop_spec.get("checksum"),
                }
                delay_ms = int(hop_spec.get("delay_ms", 0))
            if delay_ms:
                time.sleep(delay_ms / 1000.0)
            call = {
                "connection_id": connection_id,
                "hop": hop,
                "response_status_code": status,
                "digest": response.get("digest"),
                "request_table_version": request.headers.get("X-Table-Version"),
                "request_worker": request.headers.get("X-Worker-Id"),
            }
            registry.end(key, call)
            return jsonify(response), status
        except BaseException:
            with registry.lock:
                count = registry.inflight.get(key, 1) - 1
                if count <= 0:
                    registry.inflight.pop(key, None)
                else:
                    registry.inflight[key] = count
            raise

    return app


def start_loopback_server(plan: dict[str, Any]) -> tuple[Any, str, CallRegistry]:
    logging.getLogger("werkzeug").setLevel(logging.ERROR)
    registry = CallRegistry()
    app = create_app(plan, registry)
    server = make_server("127.0.0.1", 0, app, threaded=True)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base_url = f"http://127.0.0.1:{server.server_port}"
    return server, base_url, registry
