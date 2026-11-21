from __future__ import annotations

import argparse
import json
import re
import signal
import socket
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, unquote, urlparse

from .errors import WorkbenchError
from .scenario import Scenario, load_scenario
from .simulator import simulate_response


WORK_PATH = re.compile(
    r"^/v1/tenants/(?P<tenant>[^/]+)/gradients/(?P<gradient>\d+)"
    r"/windows/(?P<window>\d+)/requests/(?P<seq>\d+)$"
)


class ProgrammableHTTPServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(
        self,
        server_address: tuple[str, int],
        scenario: Scenario,
        service_name: str,
    ) -> None:
        if ":" in server_address[0]:
            self.address_family = socket.AF_INET6
        super().__init__(server_address, ProgrammableHandler)
        self.scenario = scenario
        self.service_name = service_name
        service = next(item for item in scenario.services if item.name == service_name)
        self.service = service


class ProgrammableHandler(BaseHTTPRequestHandler):
    server: ProgrammableHTTPServer

    def log_message(self, format: str, *args: Any) -> None:
        return

    def _send_json(
        self,
        status: int,
        payload: Any,
        extra_headers: dict[str, str] | None = None,
    ) -> None:
        body = json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        for key, value in (extra_headers or {}).items():
            self.send_header(key, value)
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
        parsed = urlparse(self.path)
        if parsed.path == "/healthz":
            self._send_json(
                200,
                {
                    "ok": True,
                    "service": self.server.service_name,
                    "host": self.server.service.host,
                    "port": self.server.server_address[1],
                    "tenants": [item.name for item in self.server.service.tenants],
                },
            )
            return

        match = WORK_PATH.match(parsed.path)
        if match is None:
            self._send_json(404, {"error": "unknown route"})
            return
        query = parse_qs(parsed.query)
        tenant_name = unquote(match.group("tenant"))
        tenant = next(
            (
                item
                for item in self.server.service.tenants
                if item.name == tenant_name
            ),
            None,
        )
        if tenant is None:
            self._send_json(404, {"error": "unknown tenant"})
            return
        try:
            seed = int(query.get("seed", [self.server.scenario.seed])[0])
            concurrency = int(query.get("concurrency", ["1"])[0])
            gradient_index = int(match.group("gradient"))
            window_index = int(match.group("window"))
            request_seq = int(match.group("seq"))
        except (TypeError, ValueError):
            self._send_json(400, {"error": "invalid query or path integer"})
            return

        payload = simulate_response(
            profile=tenant.profile,
            seed=seed,
            tenant=tenant_name,
            gradient_index=gradient_index,
            window_index=window_index,
            request_seq=request_seq,
            concurrency=concurrency,
        )
        if payload["outcome"] == "connection_refused":
            self.close_connection = True
            try:
                self.connection.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            self.connection.close()
            return
        headers = {
            "X-Capbench-Latency-Ms": f"{payload['simulated_latency_ms']:.6f}",
            "X-Capbench-Outcome": str(payload["outcome"]),
        }
        if payload["malformed"]:
            body = b'{"malformed":'
            self.send_response(payload["status"])
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            for key, value in headers.items():
                self.send_header(key, value)
            self.end_headers()
            self.wfile.write(body)
            return
        self._send_json(payload["status"], payload, headers)


def serve(
    scenario: Scenario,
    *,
    service_filter: str | None = None,
    port_override: int | None = None,
) -> None:
    services = [
        item
        for item in scenario.services
        if service_filter is None or item.name == service_filter
    ]
    if not services:
        raise WorkbenchError(
            "E_BAD_SERVICE",
            f"service not found: {service_filter}",
        )
    if port_override is not None and len(services) != 1:
        raise WorkbenchError(
            "E_BAD_SERVICE",
            "--port can only be used when serving one service",
        )

    servers: list[ProgrammableHTTPServer] = []
    for service in services:
        port = port_override if port_override is not None else service.port
        try:
            server = ProgrammableHTTPServer(
                (service.host, port),
                scenario,
                service.name,
            )
        except OSError as exc:
            raise WorkbenchError(
                "E_SERVICE_BIND",
                f"cannot bind {service.host}:{port}: {exc}",
            )
        servers.append(server)

    stop_event = threading.Event()
    threads = []
    for server in servers:
        thread = threading.Thread(
            target=server.serve_forever,
            name=f"capbench-http-{server.server_address[1]}",
            daemon=True,
        )
        thread.start()
        threads.append(thread)
        print(
            json.dumps(
                {
                    "event": "listening",
                    "service": server.service_name,
                    "host": server.server_address[0],
                    "port": server.server_address[1],
                },
                sort_keys=True,
            ),
            flush=True,
        )

    def _stop(signum: int, frame: Any) -> None:
        stop_event.set()

    old_int = signal.signal(signal.SIGINT, _stop)
    old_term = signal.signal(signal.SIGTERM, _stop)
    try:
        while not stop_event.wait(0.25):
            if not all(thread.is_alive() for thread in threads):
                break
    finally:
        for server in servers:
            server.shutdown()
            server.server_close()
        for thread in threads:
            thread.join(timeout=2.0)
        signal.signal(signal.SIGINT, old_int)
        signal.signal(signal.SIGTERM, old_term)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Local programmable HTTP target service for capbench",
    )
    parser.add_argument("scenario", help="scenario JSON file")
    parser.add_argument("--service", help="serve only this service")
    parser.add_argument("--port", type=int, help="override port for a single service")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        scenario = load_scenario(args.scenario)
        serve(
            scenario,
            service_filter=args.service,
            port_override=args.port,
        )
    except WorkbenchError as exc:
        print(json.dumps(exc.as_dict(), ensure_ascii=False, sort_keys=True), file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

