from __future__ import annotations

import json
import socket
import threading
from dataclasses import dataclass, field

from .models import (
    ERROR_CONN_REFUSED,
    ERROR_CONNECTION_DROPPED,
    ERROR_STEP_TIMEOUT,
    ERROR_TRANSPORT,
)


@dataclass
class AbortController:
    aborted: bool = False
    _socket: socket.socket | None = None
    _lock: threading.Lock = field(default_factory=threading.Lock)

    def register(self, sock: socket.socket) -> None:
        with self._lock:
            self._socket = sock
            if self.aborted:
                self._close_socket()

    def abort(self) -> None:
        with self._lock:
            self.aborted = True
            self._close_socket()

    def _close_socket(self) -> None:
        sock = self._socket
        if sock is None:
            return
        try:
            sock.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        try:
            sock.close()
        except OSError:
            pass


@dataclass(frozen=True)
class HTTPResult:
    ok: bool
    error_code: str | None
    error_detail: str | None
    status: int | None
    headers: dict[str, str]
    body: bytes

    def body_text(self) -> str:
        return self.body.decode("utf-8", errors="replace")


def _parse_response(raw: bytes) -> HTTPResult:
    if not raw:
        return HTTPResult(
            ok=False,
            error_code=ERROR_CONNECTION_DROPPED,
            error_detail="connection closed before an HTTP response",
            status=None,
            headers={},
            body=b"",
        )
    header_blob, separator, body = raw.partition(b"\r\n\r\n")
    if not separator:
        return HTTPResult(
            ok=False,
            error_code=ERROR_CONNECTION_DROPPED,
            error_detail="incomplete HTTP response headers",
            status=None,
            headers={},
            body=b"",
        )
    lines = header_blob.split(b"\r\n")
    try:
        status_line = lines[0].decode("latin-1")
        parts = status_line.split(" ", 2)
        status = int(parts[1])
    except (IndexError, ValueError, UnicodeDecodeError):
        return HTTPResult(
            ok=False,
            error_code=ERROR_TRANSPORT,
            error_detail="malformed HTTP status line",
            status=None,
            headers={},
            body=body,
        )
    headers: dict[str, str] = {}
    for raw_line in lines[1:]:
        key, sep, value = raw_line.partition(b":")
        if not sep:
            continue
        headers[key.decode("latin-1").strip().lower()] = value.decode(
            "latin-1"
        ).strip()
    content_length = headers.get("content-length")
    if content_length is not None:
        try:
            expected = int(content_length)
        except ValueError:
            expected = len(body)
        body = body[:expected]
    return HTTPResult(
        ok=True,
        error_code=None,
        error_detail=None,
        status=status,
        headers=headers,
        body=body,
    )


def perform_http_get(
    *,
    host: str,
    port: int,
    path: str,
    injection_ids: tuple[str, ...],
    injection_types: tuple[str, ...],
    real_delay_ms: int,
    io_timeout_seconds: float,
    controller: AbortController,
) -> HTTPResult:
    sock: socket.socket | None = None
    try:
        sock = socket.create_connection((host, port), timeout=io_timeout_seconds)
        controller.register(sock)
        if controller.aborted:
            return HTTPResult(
                ok=False,
                error_code=ERROR_CONN_REFUSED,
                error_detail="request was aborted before transmission",
                status=None,
                headers={},
                body=b"",
            )
        sock.settimeout(io_timeout_seconds)
        request_lines = [
            f"GET {path} HTTP/1.0",
            f"Host: {host}:{port}",
            "Connection: close",
            f"X-Drill-Injections: {','.join(injection_ids)}",
            f"X-Drill-Injection-Types: {','.join(injection_types)}",
            f"X-Drill-Delay-Real-Ms: {real_delay_ms}",
            "",
            "",
        ]
        sock.sendall("\r\n".join(request_lines).encode("latin-1"))
        chunks: list[bytes] = []
        while True:
            try:
                chunk = sock.recv(65536)
            except socket.timeout:
                if controller.aborted:
                    break
                continue
            if not chunk:
                break
            chunks.append(chunk)
        raw = b"".join(chunks)
        if controller.aborted:
            return HTTPResult(
                ok=False,
                error_code=ERROR_STEP_TIMEOUT,
                error_detail="logical step deadline reached; socket actively aborted",
                status=None,
                headers={},
                body=b"",
            )
        return _parse_response(raw)
    except ConnectionRefusedError as exc:
        return HTTPResult(
            ok=False,
            error_code=ERROR_CONN_REFUSED,
            error_detail=str(exc),
            status=None,
            headers={},
            body=b"",
        )
    except (ConnectionResetError, BrokenPipeError, ConnectionAbortedError) as exc:
        if controller.aborted:
            return HTTPResult(
                ok=False,
                error_code=ERROR_STEP_TIMEOUT,
                error_detail="logical step deadline reached; socket actively aborted",
                status=None,
                headers={},
                body=b"",
            )
        return HTTPResult(
            ok=False,
            error_code=ERROR_CONNECTION_DROPPED,
            error_detail=str(exc),
            status=None,
            headers={},
            body=b"",
        )
    except OSError as exc:
        if controller.aborted:
            return HTTPResult(
                ok=False,
                error_code=ERROR_STEP_TIMEOUT,
                error_detail="logical step deadline reached; socket actively aborted",
                status=None,
                headers={},
                body=b"",
            )
        return HTTPResult(
            ok=False,
            error_code=ERROR_TRANSPORT,
            error_detail=str(exc),
            status=None,
            headers={},
            body=b"",
        )
    finally:
        if sock is not None:
            try:
                sock.close()
            except OSError:
                pass


def body_is_json(body: bytes) -> tuple[bool, str | None]:
    try:
        value = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        return False, str(exc)
    return True, type(value).__name__

