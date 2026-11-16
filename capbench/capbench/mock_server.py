"""Local loopback mock target service.

Protocol (newline-delimited JSON, one request/response per line):
  request : {"cmd": "run", "req_id": "<int>", "seed": <int>}
  receipt : {"proto": "recv-v1", "req_id": "<int>", "status": "ok|error",
             "process_ms": <int>, "server_seq": <int>,
             "server_time_ms": <int>, "error_rate_bp": <int>}
  identity: request {"cmd": "id"} ->
            {"proto": "recv-v1", "cmd": "id", "process_ms": <int>,
             "error_rate_bp": <int>, "seed": <int>, "host": ..., "port": ...}

Deterministic error rule (no RNG, replayable):
  h = (server_seq * 1103515245 + 12345 + seed) % 10000
  status = "error" iff h < error_rate_bp

Truth source: every processed request is appended as one JSON line to the
server log file AND returned as the receipt. The client only trusts the receipt;
the server log allows independent post-run audits.
"""
from __future__ import annotations

import errno
import json
import os
import socket
import threading
import time
from dataclasses import dataclass

from .errors import PortError

RECEIPT_PROTO = "recv-v1"


def _monotonic_ms() -> int:
    return time.monotonic_ns() // 1_000_000


def is_port_in_use(host: str, port: int) -> bool:
    probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    probe.settimeout(0.5)
    try:
        probe.bind((host, port))
    except OSError:
        return True
    finally:
        probe.close()
    return False


def ensure_port_free(host: str, port: int) -> None:
    if is_port_in_use(host, port):
        raise PortError(
            f"loopback port already in use: {host}:{port}",
            {"host": host, "port": port},
        )


@dataclass
class ServerConfig:
    host: str = "127.0.0.1"
    port: int = 8431
    process_ms: int = 20
    error_rate_bp: int = 100      # 1% deterministic errors
    seed: int = 20261117
    log_path: str | None = None


class MockServer:
    def __init__(self, config: ServerConfig) -> None:
        self.config = config
        self._socket: socket.socket | None = None
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._seq_lock = threading.Lock()
        self._seq = 0
        self._log_lock = threading.Lock()
        self.started_at_ms = 0
        if config.log_path:
            os.makedirs(os.path.dirname(os.path.abspath(config.log_path)),
                        exist_ok=True)

    # ------------------------------------------------------------------ lifecycle
    def start(self) -> "MockServer":
        ensure_port_free(self.config.host, self.config.port)
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            sock.bind((self.config.host, self.config.port))
        except OSError as exc:
            sock.close()
            if exc.errno in (errno.EADDRINUSE, errno.EACCES):
                raise PortError(
                    f"cannot bind loopback address: {self.config.host}:"
                    f"{self.config.port}",
                    {"host": self.config.host, "port": self.config.port,
                     "errno": exc.errno},
                ) from exc
            raise
        sock.listen(128)
        sock.settimeout(0.25)
        self._socket = sock
        self._stop.clear()
        self.started_at_ms = _monotonic_ms()
        self._thread = threading.Thread(
            target=self._accept_loop, name="mock-server-accept", daemon=True)
        self._thread.start()
        return self

    def stop(self) -> None:
        self._stop.set()
        if self._socket is not None:
            try:
                self._socket.close()
            except OSError:
                pass
        if self._thread is not None:
            self._thread.join(timeout=3)

    def __enter__(self) -> "MockServer":
        return self.start()

    def __exit__(self, *exc: object) -> None:
        self.stop()

    # ------------------------------------------------------------------ internals
    def _accept_loop(self) -> None:
        assert self._socket is not None
        while not self._stop.is_set():
            try:
                conn, _addr = self._socket.accept()
            except socket.timeout:
                continue
            except OSError:
                return
            threading.Thread(target=self._handle, args=(conn,),
                             name="mock-server-conn", daemon=True).start()

    def _handle(self, conn: socket.socket) -> None:
        conn.settimeout(5)
        buffer = b""
        try:
            while not self._stop.is_set():
                chunk = conn.recv(65536)
                if not chunk:
                    return
                buffer += chunk
                while b"\n" in buffer:
                    line, buffer = buffer.split(b"\n", 1)
                    if not line.strip():
                        continue
                    self._dispatch(conn, line)
        except (OSError, socket.timeout):
            return
        finally:
            try:
                conn.close()
            except OSError:
                pass

    def _dispatch(self, conn: socket.socket, line: bytes) -> None:
        try:
            req = json.loads(line.decode("utf-8"))
        except json.JSONDecodeError:
            self._send(conn, {"proto": RECEIPT_PROTO, "status": "error",
                              "reason": "bad_json"})
            return
        if req.get("cmd") == "id":
            self._send(conn, {
                "proto": RECEIPT_PROTO, "cmd": "id",
                "process_ms": self.config.process_ms,
                "error_rate_bp": self.config.error_rate_bp,
                "seed": self.config.seed,
                "host": self.config.host, "port": self.config.port,
            })
            return
        if req.get("cmd") == "run":
            time.sleep(self.config.process_ms / 1000.0)
            with self._seq_lock:
                self._seq += 1
                seq = self._seq
            h = (seq * 1103515245 + 12345 + self.config.seed) % 10000
            status = "error" if h < self.config.error_rate_bp else "ok"
            receipt = {
                "proto": RECEIPT_PROTO,
                "req_id": req.get("req_id"),
                "status": status,
                "process_ms": self.config.process_ms,
                "server_seq": seq,
                "server_time_ms": _monotonic_ms() - self.started_at_ms,
                "error_rate_bp": self.config.error_rate_bp,
            }
            self._append_server_log(receipt)
            self._send(conn, receipt)

    def _send(self, conn: socket.socket, payload: dict) -> None:
        try:
            conn.sendall((json.dumps(payload, sort_keys=True) + "\n").encode())
        except OSError:
            pass

    def _append_server_log(self, receipt: dict) -> None:
        if not self.config.log_path:
            return
        line = json.dumps(receipt, sort_keys=True) + "\n"
        with self._log_lock:
            with open(self.config.log_path, "a", encoding="utf-8") as fh:
                fh.write(line)


def read_server_log(path: str) -> list[dict]:
    if not os.path.isfile(path):
        return []
    rows: list[dict] = []
    with open(path, "r", encoding="utf-8") as fh:
        for line in fh:
            if line.strip():
                rows.append(json.loads(line))
    return rows


def main() -> None:
    import argparse
    parser = argparse.ArgumentParser(description="Run the capbench mock target.")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8431)
    parser.add_argument("--process-ms", type=int, default=20)
    parser.add_argument("--error-rate-bp", type=int, default=100)
    parser.add_argument("--seed", type=int, default=20261117)
    parser.add_argument("--log", default=None)
    args = parser.parse_args()
    cfg = ServerConfig(host=args.host, port=args.port, process_ms=args.process_ms,
                       error_rate_bp=args.error_rate_bp, seed=args.seed,
                       log_path=args.log)
    server = MockServer(cfg).start()
    print(f"mock server listening on {cfg.host}:{cfg.port} "
          f"(process_ms={cfg.process_ms}, error_rate_bp={cfg.error_rate_bp})")
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        server.stop()
        print("mock server stopped")


if __name__ == "__main__":
    main()
