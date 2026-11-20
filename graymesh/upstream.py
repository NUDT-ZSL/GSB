"""Programmable loopback HTTP upstream for local testing.

A small threaded socket server (no external deps) that answers each request
according to the first matching route rule:
  {"prefix": "/ok",   "status": 200, "body": "hello", "delay_ms": 0}
  mode "normal":     well-formed HTTP/1.1 response with the configured status
  mode "malformed":  syntactically invalid response headers
  mode "disconnect": connection closed without any response
"""

import socket
import threading
import time


class ProgrammableUpstream:
    def __init__(self, host, port, routes, name="upstream"):
        self.host = host
        self.port = int(port)
        self.routes = list(routes)
        self.name = name
        self.request_count = 0
        self._count_lock = threading.Lock()
        self._sock = None
        self._thread = None
        self._stopped = threading.Event()

    def start(self):
        self._sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._sock.bind((self.host, self.port))
        self.port = self._sock.getsockname()[1]  # resolve ephemeral port 0
        self._sock.listen(64)
        self._sock.settimeout(0.2)
        self._thread = threading.Thread(target=self._accept_loop, daemon=True,
                                        name="upstream-%s" % self.name)
        self._thread.start()
        return self

    def stop(self):
        self._stopped.set()
        if self._sock is not None:
            try:
                self._sock.close()
            except OSError:
                pass
        if self._thread is not None:
            self._thread.join(timeout=2)

    def __enter__(self):
        return self.start()

    def __exit__(self, *exc):
        self.stop()
        return False

    def _accept_loop(self):
        while not self._stopped.is_set():
            try:
                conn, _ = self._sock.accept()
            except socket.timeout:
                continue
            except OSError:
                break
            threading.Thread(target=self._handle, args=(conn,), daemon=True).start()

    def _match(self, path):
        for rule in self.routes:
            if path.startswith(rule.get("prefix", "/")):
                return rule
        return {"status": 404, "body": "no route", "mode": "normal", "delay_ms": 0}

    def _handle(self, conn):
        with self._count_lock:
            self.request_count += 1
        try:
            conn.settimeout(5)
            data = b""
            while b"\r\n\r\n" not in data and len(data) < 65536:
                chunk = conn.recv(4096)
                if not chunk:
                    break
                data += chunk
            try:
                request_line = data.split(b"\r\n", 1)[0].decode("latin-1")
                path = request_line.split(" ")[1]
            except (IndexError, UnicodeDecodeError):
                path = "/"
            rule = self._match(path)
            delay_ms = int(rule.get("delay_ms", 0))
            if delay_ms > 0:
                time.sleep(delay_ms / 1000.0)
            mode = rule.get("mode", "normal")
            if mode == "disconnect":
                return  # close without responding
            if mode == "malformed":
                conn.sendall(b"HTTP/1.1 200 OK\r\nBad Header Line\r\n\r\n")
                return
            status = int(rule.get("status", 200))
            body = str(rule.get("body", "ok")).encode("utf-8")
            reason = {200: "OK", 201: "Created", 500: "Internal Server Error",
                      503: "Service Unavailable"}.get(status, "Status")
            head = ("HTTP/1.1 %d %s\r\nContent-Length: %d\r\nConnection: close\r\n\r\n"
                    % (status, reason, len(body))).encode("latin-1")
            conn.sendall(head + body)
        except OSError:
            pass
        finally:
            try:
                conn.close()
            except OSError:
                pass
