from __future__ import annotations

import argparse
import ipaddress
import json
import os
import signal
import socketserver
import sys
import threading
import time
from pathlib import Path


def parse_listen(value: str) -> tuple[str, int]:
    host, port_text = value.rsplit(":", 1)
    port = int(port_text)
    address = ipaddress.ip_address(host)
    if not address.is_loopback:
        raise ValueError(f"listen address must be loopback: {value}")
    return host, port


class EchoHandler(socketserver.StreamRequestHandler):
    def handle(self) -> None:
        mode = self.server.mode  # type: ignore[attr-defined]
        if mode == "close":
            self.request.shutdown(1)
            return
        while True:
            line = self.rfile.readline(4096)
            if not line:
                return
            if mode == "malformed":
                self.wfile.write(b"BAD_RESPONSE\n")
                self.wfile.flush()
                return
            self.wfile.write(line)
            self.wfile.flush()


class ThreadedServer(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True


def main() -> int:
    parser = argparse.ArgumentParser(description="Loopback echo probe service")
    parser.add_argument(
        "--listen",
        action="append",
        required=True,
        help="loopback address, for example 127.0.0.1:19091",
    )
    parser.add_argument(
        "--mode",
        choices=["echo", "malformed", "close"],
        default="echo",
    )
    parser.add_argument("--ready-file")
    args = parser.parse_args()

    servers: list[ThreadedServer] = []
    threads: list[threading.Thread] = []
    bound: list[dict[str, object]] = []
    for listen in args.listen:
        host, port = parse_listen(listen)
        server = ThreadedServer((host, port), EchoHandler)
        server.mode = args.mode  # type: ignore[attr-defined]
        servers.append(server)
        bound.append(
            {
                "host": host,
                "port": server.server_address[1],
                "mode": args.mode,
            }
        )
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        threads.append(thread)

    ready = {
        "ready": True,
        "listeners": bound,
        "pid": os.getpid(),
    }
    if args.ready_file:
        path = Path(args.ready_file)
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_suffix(path.suffix + ".tmp")
        temporary.write_text(json.dumps(ready, sort_keys=True), encoding="utf-8")
        temporary.replace(path)
    print(json.dumps(ready, sort_keys=True), flush=True)

    stopping = threading.Event()

    def stop(signum: int, frame: object) -> None:
        del signum, frame
        stopping.set()

    signal.signal(signal.SIGINT, stop)
    if hasattr(signal, "SIGTERM"):
        signal.signal(signal.SIGTERM, stop)
    try:
        while not stopping.wait(0.2):
            if any(not thread.is_alive() for thread in threads):
                break
    finally:
        for server in servers:
            server.shutdown()
            server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
