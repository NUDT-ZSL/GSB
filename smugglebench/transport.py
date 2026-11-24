"""Real loopback TCP exchange and HTTP response observation."""

from __future__ import annotations

import json
import socket
import time
from dataclasses import dataclass
from typing import Any, Callable

from .codes import (
    E_ECHO_UNREACHABLE,
    E_PEER_CLOSED,
    E_TIMEOUT,
    E_UNEXPECTED_STATUS,
    E_UNEXPECTED_RESPONSE,
)
from .util import remaining_seconds


@dataclass
class TransportResult:
    submit_monotonic_ns: int
    visible_monotonic_ns: int
    latency_ms: float
    status_code: int | None
    echo: dict[str, Any] | None
    response_bytes_seen: int
    peer_outcome: str
    error: dict[str, Any] | None


def send_and_observe(
    payload: bytes,
    *,
    host: str,
    port: int,
    deadline_ns: int,
    on_bound: Callable[[int], None] | None = None,
) -> TransportResult:
    submit_ns = time.monotonic_ns()
    response = bytearray()
    status_code: int | None = None
    echo: dict[str, Any] | None = None
    error: dict[str, Any] | None = None
    peer_outcome = "UNKNOWN"
    sock: socket.socket | None = None
    connected = False
    try:
        timeout = remaining_seconds(deadline_ns)
        if timeout <= 0:
            raise TimeoutError("connection deadline expired before send")
        family = socket.AF_INET6 if ":" in host else socket.AF_INET
        sock = socket.socket(family, socket.SOCK_STREAM)
        sock.settimeout(timeout)
        if on_bound is not None:
            sock.bind((host, 0))
            on_bound(int(sock.getsockname()[1]))
        sock.connect((host, port))
        connected = True
        sock.settimeout(max(0.001, remaining_seconds(deadline_ns)))
        sock.sendall(payload)
        try:
            sock.shutdown(socket.SHUT_WR)
        except OSError:
            pass

        while True:
            remaining = remaining_seconds(deadline_ns)
            if remaining <= 0:
                raise TimeoutError("connection deadline expired while awaiting echo")
            sock.settimeout(remaining)
            chunk = sock.recv(65536)
            if not chunk:
                break
            response.extend(chunk)
            header_end = _header_end(response)
            if header_end is not None and status_code is None:
                parsed_status, content_length, parse_error = _response_head(
                    bytes(response[:header_end[0]])
                )
                status_code = parsed_status
                if parse_error:
                    error = {
                        "code": E_UNEXPECTED_RESPONSE,
                        "message": parse_error,
                    }
                if content_length is not None:
                    body_so_far = len(response) - header_end[1]
                    if body_so_far >= content_length:
                        response = response[: header_end[1] + content_length]
                        break
        if not response:
            peer_outcome = "CLOSED_WITHOUT_RESPONSE"
            if error is None:
                error = {
                    "code": E_PEER_CLOSED,
                    "message": "peer closed without an HTTP response",
                }
        else:
            header = _header_end(response)
            if header is None:
                peer_outcome = "INCOMPLETE_RESPONSE"
                if error is None:
                    error = {
                        "code": E_PEER_CLOSED,
                        "message": "peer closed during response headers",
                    }
            else:
                head_end, body_start = header
                parsed_status, content_length, parse_error = _response_head(
                    bytes(response[:head_end])
                )
                if status_code is None:
                    status_code = parsed_status
                if parse_error and error is None:
                    error = {
                        "code": E_UNEXPECTED_RESPONSE,
                        "message": parse_error,
                    }
                body = bytes(response[body_start:])
                if content_length is not None and len(body) < content_length:
                    if error is None:
                        error = {
                            "code": E_PEER_CLOSED,
                            "message": "peer closed before the full response body",
                        }
                    peer_outcome = "TRUNCATED_BODY"
                else:
                    if content_length is None:
                        payload_body = body
                    else:
                        payload_body = body[:content_length]
                    try:
                        decoded = json.loads(payload_body.decode("utf-8"))
                        if isinstance(decoded, dict):
                            echo = decoded
                            peer_outcome = "OBSERVED"
                        else:
                            raise ValueError("echo response is not a JSON object")
                    except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
                        if error is None:
                            error = {
                                "code": E_UNEXPECTED_RESPONSE,
                                "message": f"invalid echo response body: {exc}",
                            }
                        peer_outcome = "INVALID_RESPONSE"
                if status_code != 200 and error is None:
                    error = {
                        "code": E_UNEXPECTED_STATUS,
                        "message": "echo service returned a non-success status",
                        "status_code": status_code,
                    }
    except (ConnectionRefusedError, socket.gaierror) as exc:
        peer_outcome = "UNREACHABLE"
        error = {
            "code": E_ECHO_UNREACHABLE,
            "message": str(exc),
        }
    except (TimeoutError, socket.timeout) as exc:
        peer_outcome = "TIMEOUT"
        error = {
            "code": E_TIMEOUT,
            "message": str(exc),
        }
    except ConnectionResetError as exc:
        peer_outcome = "RESET"
        error = {
            "code": E_PEER_CLOSED if connected else E_ECHO_UNREACHABLE,
            "message": str(exc),
        }
    except OSError as exc:
        if connected:
            peer_outcome = "IO_ERROR_AFTER_RESPONSE"
            error = {"code": E_PEER_CLOSED, "message": str(exc)}
        else:
            peer_outcome = "UNREACHABLE"
            error = {"code": E_ECHO_UNREACHABLE, "message": str(exc)}
    finally:
        if sock is not None:
            try:
                sock.close()
            except OSError:
                pass
    visible_ns = time.monotonic_ns()
    return TransportResult(
        submit_monotonic_ns=submit_ns,
        visible_monotonic_ns=visible_ns,
        latency_ms=(visible_ns - submit_ns) / 1_000_000.0,
        status_code=status_code,
        echo=echo,
        response_bytes_seen=len(response),
        peer_outcome=peer_outcome,
        error=error,
    )


def _header_end(data: bytes | bytearray) -> tuple[int, int] | None:
    crlf = data.find(b"\r\n\r\n")
    lf = data.find(b"\n\n")
    if crlf < 0 and lf < 0:
        return None
    if crlf >= 0 and (lf < 0 or crlf <= lf):
        return crlf, crlf + 4
    return lf, lf + 2


def _response_head(head: bytes) -> tuple[int | None, int | None, str | None]:
    lines = head.replace(b"\r\n", b"\n").split(b"\n")
    if not lines:
        return None, None, "empty HTTP response"
    parts = lines[0].decode("latin-1", errors="replace").split()
    if len(parts) < 2 or not parts[0].startswith("HTTP/"):
        return None, None, "malformed HTTP status line"
    try:
        status = int(parts[1])
    except ValueError:
        return None, None, "malformed HTTP status code"
    content_length: int | None = None
    for line in lines[1:]:
        if b":" not in line:
            continue
        name, value = line.split(b":", 1)
        if name.strip().lower() == b"content-length":
            try:
                content_length = int(value.strip())
            except ValueError:
                return status, None, "malformed response Content-Length"
    return status, content_length, None
