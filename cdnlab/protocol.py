"""JSON-lines protocol over loopback TCP (stdlib socket + threading only).

One connection carries exactly one request line and one response line.
"""

from __future__ import annotations

import json
import socket
import threading

HOST = "127.0.0.1"
RECV_LIMIT = 4 * 1024 * 1024


def send_request(port, payload, timeout=5.0):
    """Send one JSON request and return the decoded JSON response."""
    with socket.create_connection((HOST, port), timeout=timeout) as sock:
        line = (json.dumps(payload, ensure_ascii=False) + "\n").encode("utf-8")
        sock.sendall(line)
        chunks = []
        while True:
            data = sock.recv(65536)
            if not data:
                break
            chunks.append(data)
            if b"\n" in data:
                break
    raw = b"".join(chunks).split(b"\n", 1)[0]
    if not raw:
        raise ConnectionError("empty response from peer")
    return json.loads(raw.decode("utf-8"))


def _read_line(conn):
    buf = bytearray()
    while True:
        chunk = conn.recv(65536)
        if not chunk:
            break
        buf.extend(chunk)
        if b"\n" in chunk:
            break
        if len(buf) > RECV_LIMIT:
            raise ValueError("request too large")
    return bytes(buf).split(b"\n", 1)[0]


class JsonLineServer:
    """Thread-per-connection JSON-lines server bound to loopback."""

    def __init__(self, port, handler, name="server"):
        self.port = port
        self.handler = handler
        self.name = name
        self._sock = None
        self._stop = threading.Event()
        self._threads = []

    def start(self):
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.bind((HOST, self.port))
        sock.listen(64)
        sock.settimeout(0.5)
        self._sock = sock
        self.port = sock.getsockname()[1]
        thread = threading.Thread(target=self._accept_loop, name=f"{self.name}-accept", daemon=True)
        thread.start()
        return self.port

    def _accept_loop(self):
        while not self._stop.is_set():
            try:
                conn, _addr = self._sock.accept()
            except socket.timeout:
                continue
            except OSError:
                break
            thread = threading.Thread(target=self._serve, args=(conn,), daemon=True)
            thread.start()
            self._threads.append(thread)

    def _serve(self, conn):
        try:
            try:
                raw = _read_line(conn)
                if not raw:
                    return
                request = json.loads(raw.decode("utf-8"))
                response = self.handler(request)
            except ConnectionAbortedError:
                return  # injected drop: close without answering
            except Exception as exc:  # keep protocol errors structured
                response = {"ok": False, "code": "E_INTERNAL", "message": str(exc)}
            try:
                conn.sendall((json.dumps(response, ensure_ascii=False) + "\n").encode("utf-8"))
            except OSError:
                pass
        finally:
            try:
                conn.close()
            except OSError:
                pass

    def stop(self):
        self._stop.set()
        if self._sock is not None:
            try:
                self._sock.close()
            except OSError:
                pass
