"""Minimal real HTTP probing with total-budget timeout and active abort."""

from __future__ import annotations

import errno
import hashlib
import json
import re
import socket
import time
from typing import Any

from .analytics import ERROR_BY_CLASSIFICATION, stable_check_payload
from .errors import (
    E_CONN_REFUSED,
    E_MALFORMED_RESPONSE,
    E_PROBE_IO,
    E_SAMPLE_MISMATCH,
    E_STATUS_MISMATCH,
    E_TIMEOUT,
)
from .model import InstanceConfig, ScheduledCheck

STATUS_LINE_RE = re.compile(rb"^HTTP/([0-9]\.[0-9]) ([0-9]{3})(?: [^\r\n]*)?$")
HEADER_NAME_RE = re.compile(rb"^[!#$%&'*+\-.^_`|~0-9A-Za-z]+$")
MAX_RESPONSE_BYTES = 1024 * 1024


class ProbeFailure(Exception):
    def __init__(self, code: str, detail: str, *, classification: str = "malformed_response"):
        super().__init__(detail)
        self.code = code
        self.detail = detail
        self.classification = classification


def _remaining(deadline: float) -> float:
    left = deadline - time.monotonic()
    if left <= 0:
        raise socket.timeout("probe budget exhausted")
    return left


def _recv_until(
    sock: socket.socket,
    marker: bytes,
    deadline: float,
    *,
    initial: bytes = b"",
) -> bytes:
    data = bytearray(initial)
    while marker not in data:
        if len(data) > MAX_RESPONSE_BYTES:
            raise ProbeFailure(E_MALFORMED_RESPONSE, "response headers exceed size limit")
        sock.settimeout(_remaining(deadline))
        chunk = sock.recv(8192)
        if not chunk:
            break
        data.extend(chunk)
    return bytes(data)


def _recv_to_close(sock: socket.socket, deadline: float, initial: bytes = b"") -> bytes:
    data = bytearray(initial)
    while True:
        if len(data) > MAX_RESPONSE_BYTES:
            raise ProbeFailure(E_MALFORMED_RESPONSE, "response body exceeds size limit")
        sock.settimeout(_remaining(deadline))
        chunk = sock.recv(8192)
        if not chunk:
            break
        data.extend(chunk)
    return bytes(data)


def _parse_headers(header_bytes: bytes) -> tuple[int, dict[str, str]]:
    lines = header_bytes.split(b"\r\n")
    if not lines or not lines[0]:
        raise ProbeFailure(E_MALFORMED_RESPONSE, "empty HTTP status line")
    matched = STATUS_LINE_RE.match(lines[0])
    if not matched:
        raise ProbeFailure(E_MALFORMED_RESPONSE, "invalid HTTP status line")
    status = int(matched.group(2))
    headers: dict[str, str] = {}
    for line in lines[1:]:
        if not line:
            continue
        if b":" not in line:
            raise ProbeFailure(E_MALFORMED_RESPONSE, "invalid HTTP header line")
        name, value = line.split(b":", 1)
        if not HEADER_NAME_RE.match(name):
            raise ProbeFailure(E_MALFORMED_RESPONSE, "invalid HTTP header name")
        key = name.decode("ascii").lower()
        text = value.decode("latin-1").strip()
        if key in headers:
            headers[key] = f"{headers[key]}, {text}"
        else:
            headers[key] = text
    return status, headers


def _decode_chunked(body: bytes) -> bytes:
    position = 0
    output = bytearray()
    while True:
        line_end = body.find(b"\r\n", position)
        if line_end < 0:
            raise ProbeFailure(E_MALFORMED_RESPONSE, "truncated chunk size")
        size_text = body[position:line_end].split(b";", 1)[0].strip()
        try:
            size = int(size_text, 16)
        except ValueError as exc:
            raise ProbeFailure(E_MALFORMED_RESPONSE, "invalid chunk size") from exc
        position = line_end + 2
        if size == 0:
            return bytes(output)
        if position + size + 2 > len(body):
            raise ProbeFailure(E_MALFORMED_RESPONSE, "truncated chunk body")
        output.extend(body[position : position + size])
        position += size
        if body[position : position + 2] != b"\r\n":
            raise ProbeFailure(E_MALFORMED_RESPONSE, "invalid chunk terminator")
        position += 2
        if len(output) > MAX_RESPONSE_BYTES:
            raise ProbeFailure(E_MALFORMED_RESPONSE, "decoded body exceeds size limit")


def _read_response(sock: socket.socket, deadline: float) -> tuple[int, bytes]:
    received = _recv_until(sock, b"\r\n\r\n", deadline)
    if b"\r\n\r\n" not in received:
        raise ProbeFailure(E_MALFORMED_RESPONSE, "truncated HTTP headers")
    header_bytes, body = received.split(b"\r\n\r\n", 1)
    status, headers = _parse_headers(header_bytes)
    transfer_encoding = headers.get("transfer-encoding", "").lower()
    if "chunked" in transfer_encoding:
        body = _decode_chunked(_recv_to_close(sock, deadline, body))
    elif "content-length" in headers:
        values = {item.strip() for item in headers["content-length"].split(",") if item.strip()}
        if len(values) != 1:
            raise ProbeFailure(E_MALFORMED_RESPONSE, "conflicting Content-Length headers")
        value = next(iter(values))
        if not value.isdigit():
            raise ProbeFailure(E_MALFORMED_RESPONSE, "invalid Content-Length")
        length = int(value)
        if length > MAX_RESPONSE_BYTES:
            raise ProbeFailure(E_MALFORMED_RESPONSE, "declared body exceeds size limit")
        while len(body) < length:
            sock.settimeout(_remaining(deadline))
            chunk = sock.recv(min(8192, length - len(body)))
            if not chunk:
                raise ProbeFailure(E_MALFORMED_RESPONSE, "truncated response body")
            body += chunk
        body = body[:length]
    else:
        body = _recv_to_close(sock, deadline, body)
    return status, body


def probe_instance(
    instance: InstanceConfig,
    scheduled: ScheduledCheck,
    timeout_ms: int,
) -> dict[str, Any]:
    """Perform one real loopback request.

    ``time.monotonic`` is used only to enforce the real socket budget. Logical
    scheduling and all persisted timestamps use the injected logical clock.
    """

    address = instance.address
    started = time.monotonic()
    deadline = started + timeout_ms / 1000.0
    sock: socket.socket | None = None
    abort_requested = False
    status: int | None = None
    body = b""
    response_parsed = False
    classification = "success"
    detail_code: str | None = None
    try:
        sock = socket.create_connection(
            (address.host, address.port), timeout=max(0.001, _remaining(deadline))
        )
        host_header = address.authority
        request = (
            f"GET {address.path} HTTP/1.1\r\n"
            f"Host: {host_header}\r\n"
            "User-Agent: lbdiag/1.0\r\n"
            "Accept: */*\r\n"
            "Connection: close\r\n"
            "\r\n"
        ).encode("ascii")
        sock.settimeout(_remaining(deadline))
        sock.sendall(request)
        status, body = _read_response(sock, deadline)
        response_parsed = True
        if time.monotonic() > deadline:
            abort_requested = True
            raise socket.timeout("response completed after the check budget")
        if status != instance.expected_status:
            classification = "status_mismatch"
            detail_code = E_STATUS_MISMATCH
        else:
            body_digest = hashlib.sha256(body).hexdigest()
            if instance.expected_body_sha256 and body_digest != instance.expected_body_sha256:
                classification = "sample_mismatch"
                detail_code = E_SAMPLE_MISMATCH
    except socket.timeout:
        classification = "timeout"
        detail_code = E_TIMEOUT
        abort_requested = True
    except ConnectionRefusedError:
        classification = "connection_refused"
        detail_code = E_CONN_REFUSED
    except ProbeFailure as exc:
        classification = exc.classification
        detail_code = exc.code
    except OSError as exc:
        if exc.errno in {errno.ECONNREFUSED, getattr(errno, "WSAECONNREFUSED", 10061)}:
            classification = "connection_refused"
            detail_code = E_CONN_REFUSED
        else:
            classification = "malformed_response"
            detail_code = E_PROBE_IO
    finally:
        if sock is not None:
            try:
                if abort_requested:
                    sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            try:
                sock.close()
            except OSError:
                pass

    elapsed_us = max(0, int((time.monotonic() - started) * 1_000_000))
    body_digest = hashlib.sha256(body).hexdigest() if response_parsed else None
    outcome = {
        "instance_id": instance.instance_id,
        "check_index": scheduled.check_index,
        "scheduled_at_ms": scheduled.scheduled_at_ms,
        "jitter_offset_ms": scheduled.jitter_offset_ms,
        "classification": classification,
        "error_code": detail_code or ERROR_BY_CLASSIFICATION[classification],
        "http_status": status,
        "expected_status": instance.expected_status,
        "body_sha256": body_digest,
        "expected_body_sha256": instance.expected_body_sha256,
        "response_bytes": len(body),
        "abort_requested": abort_requested,
        "detail_code": detail_code,
        "measured_elapsed_us": elapsed_us,
    }
    outcome["stable_sha256"] = hashlib.sha256(
        json.dumps(
            stable_check_payload(outcome),
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()
    return outcome

