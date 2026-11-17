"""Loopback TCP server speaking one JSON object per line (NDJSON)."""

from __future__ import annotations

import json
import socketserver
import threading
from pathlib import Path
from typing import Any

from .engine import GatewayEngine
from .errors import E_SPEC, BusinessError
from .ledger import Ledger
from .loader import load_routes, load_token_table


class GatewayHandler(socketserver.StreamRequestHandler):
    def handle(self) -> None:
        server: GatewayServer = self.server  # type: ignore[assignment]
        with server.engine_lock:
            self._handle_locked(server)

    def _handle_locked(self, server: "GatewayServer") -> None:
        for raw_line in self.rfile:
            line = raw_line.strip()
            if not line:
                continue
            try:
                payload = json.loads(line)
            except json.JSONDecodeError as exc:
                response = {"code": E_SPEC, "error": f"invalid JSON: {exc.msg}"}
                self.wfile.write(json.dumps(response).encode("utf-8") + b"\n")
                self.wfile.flush()
                continue
            try:
                request = _payload_to_request(payload)
                _validate_one(request, server.engine)
                row = server.engine.process(request)
                server.ledger.log_event(
                    row["decision"], "tcp_request", {"seq": request.seq, "route": request.route}
                )
                response = {
                    "code": row["decision"],
                    "seq": request.seq,
                    "selected_path": row.get("selected_path"),
                    "remaining_quota": row.get("remaining_quota"),
                    "paths": {
                        name: {"allow": item["allow"], "remaining": item["remaining"]}
                        for name, item in row["paths"].items()
                    },
                }
            except BusinessError as exc:
                response = {"code": exc.code, "error": exc.message}
            self.wfile.write(json.dumps(response).encode("utf-8") + b"\n")
            self.wfile.flush()


def _payload_to_request(payload: dict[str, Any]):
    from .models import Request

    required = ("seq", "route", "client_id", "token", "ts_ms")
    missing = [name for name in required if name not in payload]
    if missing:
        raise BusinessError(E_SPEC, f"missing fields {missing}")
    return Request(
        seq=int(payload["seq"]),
        route=str(payload["route"]),
        client_id=str(payload["client_id"]),
        token=str(payload["token"]),
        ts_ms=int(payload["ts_ms"]),
        signed_at_ms=payload.get("signed_at_ms"),
        signature=payload.get("signature"),
    )


def _validate_one(request, engine: GatewayEngine) -> None:
    if request.route not in engine.routes_by_name:
        raise BusinessError(E_SPEC, f"unknown route {request.route!r}")
    if request.ts_ms < 0:
        raise BusinessError(E_SPEC, "ts_ms must be non-negative")
    if request.seq < 1:
        raise BusinessError(E_SPEC, "seq must be positive")


class GatewayServer(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True

    def __init__(self, port: int, workdir: str | Path, routes_path: str | Path,
                 token_path: str | Path) -> None:
        workdir = Path(workdir)
        workdir.mkdir(parents=True, exist_ok=True)
        routes_version, routes = load_routes(routes_path)
        table = load_token_table(token_path)
        self.engine = GatewayEngine(routes, routes_version, table)
        self.ledger = Ledger(workdir / "ledger.db")
        self.ledger.save_policy_snapshot(self.engine.current_version, routes, table)
        self.engine_lock = threading.Lock()
        super().__init__(("127.0.0.1", port), GatewayHandler)

    def shutdown(self) -> None:  # pragma: no cover - exercised by demo only
        self.ledger.close()
        super().shutdown()


def run_server(port: int, workdir: str, routes_path: str, token_path: str) -> GatewayServer:
    server = GatewayServer(port, workdir, routes_path, token_path)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server
