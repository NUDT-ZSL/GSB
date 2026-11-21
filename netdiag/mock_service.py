from __future__ import annotations

import argparse
import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlsplit

from .errors import BusinessError


class LocalMockService:
    """A deterministic, loopback-only HTTP fault injection service."""

    def __init__(self, config: dict[str, Any], host: str = "127.0.0.1", port: int = 0):
        if host not in {"127.0.0.1", "::1", "localhost"}:
            raise BusinessError(
                "MOCK_HOST_NON_LOOPBACK",
                "mock service must bind to a loopback host",
                {"host": host},
            )
        self.config = config
        self._server: ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None
        self._handler_cls = _build_handler(config)
        try:
            self._server = _ThreadingLoopbackServer(
                (host, port), self._handler_cls
            )
        except OSError as exc:
            raise BusinessError(
                "MOCK_BIND_FAILED",
                "cannot bind the local mock HTTP service",
                {"host": host, "port": port, "reason": str(exc)},
            ) from exc
        self._server.daemon_threads = True
        self.host, self.port = self._server.server_address[:2]

    @property
    def base_url(self) -> str:
        host = "127.0.0.1" if self.host in {"localhost", "127.0.0.1"} else self.host
        return f"http://{host}:{self.port}"

    def start(self) -> "LocalMockService":
        if self._thread is not None:
            return self
        self._thread = threading.Thread(
            target=self._server.serve_forever,
            name="netdiag-mock-http",
            daemon=True,
        )
        self._thread.start()
        return self

    def stop(self) -> None:
        if self._server is not None:
            self._server.shutdown()
            self._server.server_close()
        if self._thread is not None:
            self._thread.join(timeout=2)
        self._thread = None

    def __enter__(self) -> "LocalMockService":
        return self.start()

    def __exit__(self, *_args: object) -> None:
        self.stop()


class _ThreadingLoopbackServer(ThreadingHTTPServer):
    allow_reuse_address = True
    daemon_threads = True


def _build_handler(config: dict[str, Any]) -> type[BaseHTTPRequestHandler]:
    routes: dict[str, dict[str, Any]] = config.get("routes", {})

    class Handler(BaseHTTPRequestHandler):
        server_version = "LocalNetDiagMock/1"
        sys_version = ""

        def log_message(self, _format: str, *_args: object) -> None:
            return None

        def do_GET(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
            parsed = urlsplit(self.path)
            query = parse_qs(parsed.query)
            try:
                seq = int(query.get("netdiag_seq", ["1"])[0])
            except ValueError:
                seq = 1
            route = routes.get(parsed.path)
            if route is None:
                self._send_json(
                    404,
                    {"error": "unknown mock route", "path": parsed.path},
                    latency_ms=1,
                    seq=seq,
                )
                return

            loss = route.get("loss")
            if loss and seq % int(loss["modulus"]) in {
                int(value) for value in loss["remainders"]
            }:
                self._disconnect(loss.get("kind", "disconnect"))
                return

            latency = _latency_for(route, seq)
            actual_sleep_cap = int(config.get("actual_sleep_cap_ms", 40))
            actual_sleep = min(max(latency, 0), actual_sleep_cap) / 1000.0
            if actual_sleep:
                time.sleep(actual_sleep)

            if route.get("disconnect_after_headers"):
                self.send_response(int(route.get("status", 200)))
                self.send_header("X-NetDiag-Sim-Latency-Ms", str(latency))
                self.send_header("Content-Length", "24")
                self.send_header("Connection", "close")
                self.end_headers()
                self.wfile.flush()
                self.close_connection = True
                try:
                    self.connection.shutdown(2)
                except OSError:
                    pass
                return

            if route.get("malformed"):
                self._send_raw(
                    int(route.get("status", 200)),
                    b'{"broken":',
                    "application/json",
                    latency,
                    seq,
                )
                return

            self._send_json(
                int(route.get("status", 200)),
                route.get("body", {"ok": True}),
                latency_ms=latency,
                seq=seq,
            )

        def _disconnect(self, kind: str) -> None:
            self.close_connection = True
            try:
                self.connection.shutdown(2)
            except OSError:
                pass
            self.connection.close()

        def _send_json(
            self,
            status: int,
            body: Any,
            *,
            latency_ms: int,
            seq: int,
        ) -> None:
            payload = json.dumps(
                body, ensure_ascii=False, sort_keys=True, separators=(",", ":")
            ).encode("utf-8")
            self._send_raw(status, payload, "application/json", latency_ms, seq)

        def _send_raw(
            self,
            status: int,
            payload: bytes,
            content_type: str,
            latency_ms: int,
            seq: int,
        ) -> None:
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(payload)))
            self.send_header("X-NetDiag-Sim-Latency-Ms", str(latency_ms))
            self.send_header("X-NetDiag-Probe-Seq", str(seq))
            self.send_header("Connection", "close")
            self.end_headers()
            try:
                self.wfile.write(payload)
            except (BrokenPipeError, ConnectionResetError):
                pass

    return Handler


def _latency_for(route: dict[str, Any], seq: int) -> int:
    pattern = route.get("latency_pattern")
    if pattern:
        return int(pattern[seq % len(pattern)])
    return int(route.get("latency_ms", 0))


def load_mock_config(path: str | Path) -> dict[str, Any]:
    try:
        raw = json.loads(Path(path).read_text(encoding="utf-8"))
    except OSError as exc:
        raise BusinessError(
            "MOCK_CONFIG_READ_FAILED",
            "cannot read mock service config",
            {"path": str(path), "reason": str(exc)},
        ) from exc
    except json.JSONDecodeError as exc:
        raise BusinessError(
            "MOCK_CONFIG_INVALID_JSON",
            "mock service config is not valid JSON",
            {"path": str(path), "line": exc.lineno, "column": exc.colno},
        ) from exc
    if not isinstance(raw, dict) or not isinstance(raw.get("routes"), dict):
        raise BusinessError(
            "MOCK_CONFIG_INVALID",
            "mock service config must contain a routes object",
            {"path": str(path)},
        )
    return raw


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run the local loopback mock HTTP service")
    parser.add_argument("--config", default="samples/mock_service.json")
    parser.add_argument("--port", type=int, default=18080)
    args = parser.parse_args(argv)
    config = load_mock_config(args.config)
    service = LocalMockService(config, port=args.port).start()
    print(json.dumps({"ok": True, "base_url": service.base_url}, sort_keys=True))
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        pass
    finally:
        service.stop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
