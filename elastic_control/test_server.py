from __future__ import annotations

import argparse
import json
import signal
import threading
import time
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

from .models import canonical_json, load_json

LOOPBACK_HOSTS = {"127.0.0.1", "localhost", "::1"}


class FaultState:
    def __init__(self, config: dict[str, Any]) -> None:
        self.config = config
        self.lock = threading.RLock()
        self.rules: list[dict[str, Any]] = []
        for raw_rule in config.get("rules", []):
            rule = dict(raw_rule)
            rule["_matches"] = 0
            self.rules.append(rule)
        self.idempotency: dict[str, dict[str, Any]] = {}
        self.replicas: dict[str, dict[str, Any]] = {}
        self.requests: list[dict[str, Any]] = []
        self.idempotency_hits = 0

    def choose_rule(self, payload: dict[str, Any]) -> dict[str, Any] | None:
        with self.lock:
            for rule in self.rules:
                match = rule.get("match", {})
                if any(payload.get(key) != value for key, value in match.items()):
                    continue
                limit = rule.get("times")
                if limit is not None and rule["_matches"] >= int(limit):
                    continue
                rule["_matches"] += 1
                return rule
        return None

    def snapshot(self) -> dict[str, Any]:
        with self.lock:
            return {
                "requests": list(self.requests),
                "request_count": len(self.requests),
                "idempotency_hits": self.idempotency_hits,
                "replicas": dict(self.replicas),
                "rules": [
                    {
                        "id": rule.get("id"),
                        "matches": rule["_matches"],
                    }
                    for rule in self.rules
                ],
            }


class LocalControlHandler(BaseHTTPRequestHandler):
    server_version = "ElasticControlTestServer/1.0"

    def log_message(self, format: str, *args: Any) -> None:
        return

    def do_GET(self) -> None:
        if self.path == "/health":
            self._send_json(HTTPStatus.OK, {"ok": True})
            return
        if self.path == "/admin/state":
            self._send_json(HTTPStatus.OK, self.server.fault_state.snapshot())
            return
        self._send_json(HTTPStatus.NOT_FOUND, {"ok": False, "error": "not_found"})

    def do_POST(self) -> None:
        if self.path == "/admin/reset":
            self.server.fault_state = FaultState(self.server.fault_config)
            self._send_json(HTTPStatus.OK, {"ok": True})
            return
        if self.path not in {"/v1/replicas/launch", "/v1/replicas/drain"}:
            self._send_json(HTTPStatus.NOT_FOUND, {"ok": False, "error": "not_found"})
            return

        length_header = self.headers.get("Content-Length")
        if length_header is None:
            self._send_json(
                HTTPStatus.BAD_REQUEST,
                {"ok": False, "error_code": "MISSING_BODY"},
            )
            return
        try:
            payload = json.loads(self.rfile.read(int(length_header)).decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            self._send_json(
                HTTPStatus.BAD_REQUEST,
                {"ok": False, "error_code": "INVALID_JSON"},
            )
            return
        if not isinstance(payload, dict):
            self._send_json(
                HTTPStatus.BAD_REQUEST,
                {"ok": False, "error_code": "INVALID_PAYLOAD"},
            )
            return

        required = {
            "action",
            "replica_id",
            "service_name",
            "model_version",
            "sample_digest",
            "idempotency_key",
        }
        if not required.issubset(payload):
            self._send_json(
                HTTPStatus.BAD_REQUEST,
                {
                    "ok": False,
                    "error_code": "MISSING_FIELDS",
                    "missing": sorted(required - set(payload)),
                },
            )
            return
        endpoint_action = self.path.rsplit("/", 1)[-1]
        if payload["action"] != endpoint_action:
            self._send_json(
                HTTPStatus.BAD_REQUEST,
                {"ok": False, "error_code": "ACTION_ENDPOINT_MISMATCH"},
            )
            return

        state: FaultState = self.server.fault_state
        key = str(payload["idempotency_key"])
        with state.lock:
            cached = state.idempotency.get(key)
            if cached is not None:
                state.idempotency_hits += 1
                response = dict(cached["response"])
                response["idempotency_hit"] = True
                self._send_cached(response, int(cached["status"]), cached["raw"])
                return

        rule = state.choose_rule(payload)
        delay_ms = float(
            (rule or {}).get("delay_ms", state.config.get("default_delay_ms", 0))
        )
        if delay_ms > 0:
            time.sleep(delay_ms / 1000.0)

        mode = str((rule or {}).get("mode", "ok"))
        status = int((rule or {}).get("status", 200))
        if mode == "http_500":
            status = int((rule or {}).get("status", 500))
        if mode == "http_504":
            status = int((rule or {}).get("status", 504))

        response = {
            "ok": True,
            "action": payload["action"],
            "replica_id": payload["replica_id"],
            "service_name": payload["service_name"],
            "model_version": payload["model_version"],
            "sample_digest": payload["sample_digest"],
            "tick": payload.get("tick"),
            "round_id": payload.get("round_id"),
            "idempotency_key": key,
            "idempotency_hit": False,
        }
        raw_override: str | None = None
        if mode == "version_mismatch":
            response["model_version"] = f"{payload['model_version']}--mismatch"
        elif mode == "digest_mismatch":
            response["sample_digest"] = "0" * 64
        elif mode == "remote_error":
            status = int((rule or {}).get("status", 200))
            response["ok"] = False
            response["error_code"] = str(
                (rule or {}).get("error_code", "INJECTED_REMOTE_ERROR")
            )
        elif mode == "bad_json":
            raw_override = "{not-json"
        elif mode in {"http_500", "http_504"}:
            response["ok"] = False
            response["error_code"] = mode.upper()
        elif mode == "connection_drop":
            self.close_connection = True
            try:
                self.connection.shutdown(2)
            except OSError:
                pass
            return

        custom_response = (rule or {}).get("response")
        if isinstance(custom_response, dict):
            response.update(custom_response)

        with state.lock:
            state.requests.append(
                {
                    "action": payload["action"],
                    "replica_id": payload["replica_id"],
                    "service_name": payload["service_name"],
                    "idempotency_key": key,
                    "tick": payload.get("tick"),
                    "mode": mode,
                }
            )
            state.idempotency[key] = {
                "status": status,
                "response": response,
                "raw": raw_override,
            }
            if payload["action"] == "launch" and response.get("ok") is True:
                state.replicas[str(payload["replica_id"])] = {
                    "service_name": payload["service_name"],
                    "model_version": response["model_version"],
                    "phase": "cold_starting",
                    "node_name": payload.get("node_name"),
                    "gpu_index": payload.get("gpu_index"),
                }
            elif payload["action"] == "drain" and response.get("ok") is True:
                state.replicas[str(payload["replica_id"])] = {
                    "service_name": payload["service_name"],
                    "model_version": response["model_version"],
                    "phase": "reclaimed",
                    "node_name": payload.get("node_name"),
                    "gpu_index": payload.get("gpu_index"),
                }

        self._send_cached(response, status, raw_override)

    def _send_cached(
        self,
        response: dict[str, Any],
        status: int,
        raw: str | None,
    ) -> None:
        body = raw if raw is not None else canonical_json(response)
        encoded = body.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(encoded)))
        self.end_headers()
        self.wfile.write(encoded)

    def _send_json(self, status: int, payload: dict[str, Any]) -> None:
        body = canonical_json(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


class LocalControlServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(
        self,
        address: tuple[str, int],
        fault_config: dict[str, Any],
    ) -> None:
        self.fault_config = fault_config
        self.fault_state = FaultState(fault_config)
        super().__init__(address, LocalControlHandler)


def create_server(
    host: str,
    port: int,
    fault_config: dict[str, Any] | None = None,
) -> LocalControlServer:
    if host not in LOOPBACK_HOSTS:
        raise ValueError("test server may only bind to a loopback address")
    return LocalControlServer((host, port), dict(fault_config or {}))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Local deterministic replica test server")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=0)
    parser.add_argument("--faults", help="fault injection JSON file")
    parser.add_argument("--port-file", help="write selected port to this file")
    args = parser.parse_args(argv)

    fault_config: dict[str, Any] = {}
    if args.faults:
        loaded = load_json(args.faults)
        if not isinstance(loaded, dict):
            raise SystemExit("faults JSON must be an object")
        fault_config = loaded

    server = create_server(args.host, args.port, fault_config)
    host, port = server.server_address[:2]
    if args.port_file:
        Path(args.port_file).write_text(str(port), encoding="utf-8")
    print(
        canonical_json(
            {
                "ok": True,
                "event": "listening",
                "host": host,
                "port": port,
                "url": f"http://{host}:{port}",
            }
        ),
        flush=True,
    )

    stop = threading.Event()

    def _stop(_signum: int, _frame: Any) -> None:
        stop.set()

    signal.signal(signal.SIGTERM, _stop)
    signal.signal(signal.SIGINT, _stop)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        while not stop.wait(0.1):
            pass
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

