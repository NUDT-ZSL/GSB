"""Deterministic local loopback HTTP service for health-check samples."""

from __future__ import annotations

import argparse
import json
import socket
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from .errors import BusinessError, as_business_error
from .model import parse_sample_file


class LoopbackHTTPServer(ThreadingHTTPServer):
    """Select IPv4 or IPv6 from the configured literal loopback host."""

    def __init__(self, server_address: tuple[str, int], *args: Any, **kwargs: Any) -> None:
        self.address_family = (
            socket.AF_INET6 if ":" in server_address[0] else socket.AF_INET
        )
        super().__init__(server_address, *args, **kwargs)


class RouteState:
    def __init__(self, routes: dict[str, Any]) -> None:
        self.routes = routes
        self.counters = {path: 0 for path in routes}
        self.lock = threading.Lock()

    def next(self, path: str) -> tuple[dict[str, Any], int]:
        with self.lock:
            route = self.routes.get(path)
            if route is None:
                return {"status": 404, "body": "not-found"}, 0
            count = self.counters.get(path, 0)
            self.counters[path] = count + 1
        return route, count


def _sequence_value(value: Any, index: int) -> Any:
    if isinstance(value, list):
        if not value:
            return None
        return value[index % len(value)]
    return value


class Handler(BaseHTTPRequestHandler):
    server_version = "lbdiag-test/1.0"
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt: str, *args: object) -> None:
        return

    def do_GET(self) -> None:  # noqa: N802
        state: RouteState = self.server.route_state  # type: ignore[attr-defined]
        path = urlsplit(self.path).path
        route, index = state.next(path)
        sleep_ms = _sequence_value(route.get("sleep_ms"), index) or 0
        if sleep_ms:
            time.sleep(float(sleep_ms) / 1000.0)
        if route.get("malformed"):
            payload = _sequence_value(route.get("raw"), index)
            if payload is None:
                payload = "THIS IS NOT AN HTTP RESPONSE\r\n\r\n"
            try:
                self.wfile.write(str(payload).encode("latin-1"))
                self.wfile.flush()
            except OSError:
                pass
            self.close_connection = True
            return
        status_source = route.get("status_sequence", route.get("status", 200))
        status = int(_sequence_value(status_source, index))
        body_value = _sequence_value(route.get("body", ""), index)
        if isinstance(route.get("bodies"), dict):
            mapped = route["bodies"].get(str(status))
            if mapped is not None:
                body_value = mapped
        body = str(body_value).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Connection", "close")
        self.end_headers()
        try:
            self.wfile.write(body)
            self.wfile.flush()
        except OSError:
            pass
        self.close_connection = True


def serve(sample_path: str | Path, *, host: str | None = None, port: int | None = None) -> None:
    config = parse_sample_file(sample_path)
    test_service = config.raw.get("test_service")
    if not isinstance(test_service, dict):
        raise BusinessError(
            "E_BAD_SAMPLE",
            "sample has no test_service section",
            fields={"sample": str(sample_path)},
        )
    bind_host = host or str(test_service.get("host", "127.0.0.1"))
    bind_port = int(port if port is not None else test_service.get("port", 0))
    routes = test_service.get("routes", {})
    server = LoopbackHTTPServer((bind_host, bind_port), Handler)
    server.daemon_threads = True
    server.route_state = RouteState(routes)  # type: ignore[attr-defined]
    readiness = {
        "ready": True,
        "host": bind_host,
        "port": server.server_address[1],
        "routes": sorted(routes),
    }
    print(json.dumps(readiness, sort_keys=True, separators=(",", ":")), flush=True)
    try:
        server.serve_forever(poll_interval=0.1)
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run the local deterministic HTTP test service")
    parser.add_argument("--sample", required=True)
    parser.add_argument("--host")
    parser.add_argument("--port", type=int)
    args = parser.parse_args(argv)
    try:
        serve(args.sample, host=args.host, port=args.port)
        return 0
    except BusinessError as exc:
        print(json.dumps(exc.as_dict(), sort_keys=True, separators=(",", ":")))
        return exc.exit_code
    except OSError as exc:
        error = as_business_error(exc)
        print(json.dumps(error.as_dict(), sort_keys=True, separators=(",", ":")))
        return 3


if __name__ == "__main__":
    raise SystemExit(main())

