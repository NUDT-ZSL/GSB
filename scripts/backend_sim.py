#!/usr/bin/env python3
"""Deterministic loopback HTTP/TCP backend with scriptable behavior."""

from __future__ import annotations

import argparse
import ipaddress
import json
import os
import socket
import socketserver
import sys
import threading
import time
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from lbhealth.behavior import BehaviorSource  # noqa: E402


class RuntimeCounters:
    def __init__(self, start_index: int, state_file: str | None) -> None:
        self._lock = threading.Lock()
        self.request_index = start_index
        self.state_file = Path(state_file) if state_file else None
        if self.state_file is not None:
            self.state_file.parent.mkdir(parents=True, exist_ok=True)
            self._write_locked(None)

    def next_action(self, source: BehaviorSource) -> tuple[int, str, int]:
        with self._lock:
            index = self.request_index
            self.request_index += 1
            action = source.for_request(index)
            slow_ms = source.slow_ms()
            self._write_locked(action)
            return index, action, slow_ms

    def _write_locked(self, action: str | None) -> None:
        if self.state_file is None:
            return
        payload = {
            "request_index": self.request_index,
            "last_action": action,
            "pid": os.getpid(),
        }
        temp = self.state_file.with_name(
            f".{self.state_file.name}.{os.getpid()}.{threading.get_ident()}.tmp"
        )
        temp.write_text(json.dumps(payload, sort_keys=True) + "\n", encoding="utf-8")
        os.replace(temp, self.state_file)


class SimulatorServer(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True

    def __init__(
        self,
        server_address: tuple[str, int],
        handler: type[socketserver.BaseRequestHandler],
        protocol: str,
        behavior: BehaviorSource,
        counters: RuntimeCounters,
    ) -> None:
        self.protocol = protocol
        self.behavior = behavior
        self.counters = counters
        self.address_family = socket.AF_INET6 if ":" in server_address[0] else socket.AF_INET
        super().__init__(server_address, handler)


class BaseHandler(socketserver.BaseRequestHandler):
    server: SimulatorServer

    def _decide(self) -> tuple[str, int]:
        index, action, slow_ms = self.server.counters.next_action(self.server.behavior)
        return action, slow_ms if action == "slow" else 0

    def _slow(self, delay_ms: int) -> None:
        if delay_ms > 0:
            # This is a server-side response delay, never used to fabricate a
            # probe verdict; the client's real socket timeout decides failure.
            time.sleep(delay_ms / 1000.0)


class HttpHandler(BaseHandler):
    def handle(self) -> None:
        try:
            request = self._read_head()
            if not request:
                return
            action, delay_ms = self._decide()
            self._slow(delay_ms)
            if action == "close":
                return
            if action == "fail":
                body = b"unavailable\n"
                status = b"503 Service Unavailable"
            else:
                body = b"ok\n"
                status = b"200 OK"
            response = (
                b"HTTP/1.1 " + status + b"\r\n"
                b"Content-Type: text/plain; charset=utf-8\r\n"
                b"Content-Length: " + str(len(body)).encode("ascii") + b"\r\n"
                b"Connection: close\r\n\r\n" + body
            )
            self.request.sendall(response)
        except (BrokenPipeError, ConnectionResetError, OSError):
            return

    def _read_head(self) -> bytes:
        data = bytearray()
        while len(data) < 8192 and b"\r\n\r\n" not in data:
            chunk = self.request.recv(1024)
            if not chunk:
                break
            data.extend(chunk)
        return bytes(data)


class TcpHandler(BaseHandler):
    def handle(self) -> None:
        try:
            self.request.settimeout(2.0)
            data = self.request.recv(64)
            if not data:
                return
            action, delay_ms = self._decide()
            self._slow(delay_ms)
            if action == "close":
                return
            if action == "fail":
                self.request.sendall(b"FAIL\n")
            else:
                self.request.sendall(b"PONG\n")
        except (BrokenPipeError, ConnectionResetError, OSError):
            return


def _loopback_literal(value: str) -> str:
    try:
        address = ipaddress.ip_address(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("host must be an IP literal") from exc
    if not address.is_loopback:
        raise argparse.ArgumentTypeError("host must be a loopback IP literal")
    return value


def _write_ready(path: str | None, protocol: str, host: str, port: int) -> None:
    if not path:
        return
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    payload: dict[str, Any] = {
        "status": "ready",
        "protocol": protocol,
        "host": host,
        "port": port,
        "pid": os.getpid(),
    }
    temp = target.with_name(f".{target.name}.{os.getpid()}.tmp")
    temp.write_text(json.dumps(payload, sort_keys=True) + "\n", encoding="utf-8")
    os.replace(temp, target)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--protocol", choices=("http", "tcp"), required=True)
    parser.add_argument("--host", type=_loopback_literal, default="127.0.0.1")
    parser.add_argument("--port", type=int, required=True)
    parser.add_argument("--behavior", required=True)
    parser.add_argument("--start-index", type=int, default=0)
    parser.add_argument("--state-file", default=None)
    parser.add_argument("--ready-file", default=None)
    args = parser.parse_args(argv)
    if not 1 <= args.port <= 65535:
        parser.error("--port must be from 1 to 65535")
    if args.start_index < 0:
        parser.error("--start-index must be non-negative")

    source = BehaviorSource(args.behavior)
    counters = RuntimeCounters(args.start_index, args.state_file)
    handler = HttpHandler if args.protocol == "http" else TcpHandler
    server = SimulatorServer((args.host, args.port), handler, args.protocol, source, counters)
    bound_port = int(server.server_address[1])
    _write_ready(args.ready_file, args.protocol, args.host, bound_port)
    try:
        server.serve_forever(poll_interval=0.05)
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
