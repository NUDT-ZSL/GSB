"""Loopback HTTP echo peer used for real wire-level observations."""

from __future__ import annotations

import hashlib
import json
import re
import socket
import socketserver
import threading
import time
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class EchoSchedule:
    delay_ms: int = 0
    close_early: bool = False
    status: int = 200


class _EchoHandler(socketserver.BaseRequestHandler):
    def handle(self) -> None:
        server: "EchoServer" = self.server.echo_server  # type: ignore[attr-defined]
        connection: socket.socket = self.request
        connection.settimeout(server.read_timeout_s)
        chunks: list[bytes] = []
        try:
            while True:
                chunk = connection.recv(65536)
                if not chunk:
                    break
                chunks.append(chunk)
                if sum(len(item) for item in chunks) > server.max_request_bytes:
                    server.record_event("E_BUDGET", "request exceeded echo server byte cap")
                    return
        except (TimeoutError, socket.timeout, OSError):
            server.record_event("E_PEER_CLOSED", "request read ended without clean EOF")

        raw = b"".join(chunks)
        digest = hashlib.sha256(raw).hexdigest()
        schedule = server.schedule_for(digest, int(self.client_address[1]))
        if schedule.delay_ms:
            time.sleep(schedule.delay_ms / 1000.0)
        if schedule.close_early:
            server.record_event("E_PEER_CLOSED", "injected early close", digest)
            return

        interpretation = server.interpret(raw)
        payload = json.dumps(
            {
                "echo_hex": raw.hex(),
                "echo_sha256": digest,
                "interpretation": interpretation,
                "server_policy": server.policy_dict(),
            },
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        reason = {
            200: "OK",
            400: "Bad Request",
            500: "Internal Server Error",
            503: "Service Unavailable",
        }.get(schedule.status, "Observed")
        headers = (
            f"HTTP/1.1 {schedule.status} {reason}\r\n"
            "Content-Type: application/json\r\n"
            f"Content-Length: {len(payload)}\r\n"
            "Connection: close\r\n"
            "\r\n"
        ).encode("ascii")
        try:
            connection.sendall(headers + payload)
            server.record_event("OBSERVED", "echo response sent", digest)
        except OSError:
            server.record_event("E_PEER_CLOSED", "peer closed before echo response", digest)


class _ThreadingEchoServer(socketserver.ThreadingMixIn, socketserver.TCPServer):
    daemon_threads = True
    allow_reuse_address = True
    request_queue_size = 128


class EchoServer:
    """A deterministic but independently implemented local HTTP interpretation peer."""

    def __init__(
        self,
        host: str = "127.0.0.1",
        port: int = 0,
        *,
        te_cl_policy: str = "content-length",
        duplicate_cl_policy: str = "first",
        chunk_policy: str = "lenient",
        trailer_policy: str = "preserve",
        default_delay_ms: int = 0,
        read_timeout_s: float = 5.0,
        max_request_bytes: int = 8 * 1024 * 1024,
    ):
        self.host = host
        self.requested_port = port
        self.te_cl_policy = te_cl_policy
        self.duplicate_cl_policy = duplicate_cl_policy
        self.chunk_policy = chunk_policy
        self.trailer_policy = trailer_policy
        self.default_delay_ms = default_delay_ms
        self.read_timeout_s = read_timeout_s
        self.max_request_bytes = max_request_bytes
        self._server: _ThreadingEchoServer | None = None
        self._thread: threading.Thread | None = None
        self._schedule_lock = threading.Lock()
        self._schedules: dict[str, EchoSchedule] = {}
        self._events_lock = threading.Lock()
        self._events: list[dict[str, Any]] = []

    @property
    def port(self) -> int:
        if self._server is None:
            return 0
        return int(self._server.server_address[1])

    @property
    def address(self) -> tuple[str, int]:
        return self.host, self.port

    def start(self) -> "EchoServer":
        if self._server is not None:
            return self
        server = _ThreadingEchoServer((self.host, self.requested_port), _EchoHandler)
        server.echo_server = self  # type: ignore[attr-defined]
        self._server = server
        self._thread = threading.Thread(
            target=server.serve_forever,
            name=f"echo-server-{self.port}",
            daemon=True,
        )
        self._thread.start()
        return self

    def stop(self) -> None:
        if self._server is None:
            return
        server = self._server
        self._server = None
        server.shutdown()
        server.server_close()
        if self._thread is not None:
            self._thread.join(timeout=2.0)
        self._thread = None

    def __enter__(self) -> "EchoServer":
        return self.start()

    def __exit__(self, *_exc: object) -> None:
        self.stop()

    def register(
        self,
        payload: bytes,
        *,
        delay_ms: int | None = None,
        close_early: bool = False,
        status: int = 200,
        source_port: int | None = None,
    ) -> EchoSchedule:
        if delay_ms is not None and delay_ms < 0:
            raise ValueError("delay_ms must be non-negative")
        schedule = EchoSchedule(
            delay_ms=self.default_delay_ms if delay_ms is None else delay_ms,
            close_early=close_early,
            status=status,
        )
        digest = hashlib.sha256(payload).hexdigest()
        key = f"{source_port}:{digest}" if source_port is not None else digest
        with self._schedule_lock:
            self._schedules[key] = schedule
        return schedule

    def schedule_for(self, digest: str, source_port: int | None = None) -> EchoSchedule:
        with self._schedule_lock:
            exact_key = f"{source_port}:{digest}" if source_port is not None else digest
            schedule = self._schedules.get(exact_key)
            if schedule is None:
                schedule = self._schedules.get(
                    digest,
                    EchoSchedule(delay_ms=self.default_delay_ms),
                )
            return schedule

    def record_event(self, code: str, message: str, digest: str | None = None) -> None:
        with self._events_lock:
            self._events.append(
                {
                    "code": code,
                    "message": message,
                    "request_sha256": digest,
                    "monotonic_ns": time.monotonic_ns(),
                }
            )

    def events(self) -> list[dict[str, Any]]:
        with self._events_lock:
            return list(self._events)

    def clear_schedules(self) -> None:
        with self._schedule_lock:
            self._schedules.clear()

    def policy_dict(self) -> dict[str, Any]:
        return {
            "te_cl_policy": self.te_cl_policy,
            "duplicate_cl_policy": self.duplicate_cl_policy,
            "chunk_policy": self.chunk_policy,
            "trailer_policy": self.trailer_policy,
        }

    def interpret(self, raw: bytes) -> dict[str, Any]:
        head_end, separator_length = _find_head_end(raw)
        if head_end is None:
            return {
                "framing": "UNPARSEABLE",
                "framing_candidates": ["UNPARSEABLE"],
                "request_boundary": [0, len(raw)],
                "next_boundary": 0,
                "body_length": 0,
                "chunks": [],
                "trailers": [],
                "duplicate_headers": {},
                "complete": False,
            }
        head = raw[:head_end]
        body_start = head_end + separator_length
        lines = head.replace(b"\r\n", b"\n").split(b"\n")
        start_line = lines[0].decode("latin-1", errors="replace") if lines else ""
        headers: list[tuple[str, str]] = []
        for line in lines[1:]:
            if not line or b":" not in line:
                continue
            name, value = line.split(b":", 1)
            try:
                header_name = name.decode("ascii").strip()
            except UnicodeDecodeError:
                continue
            if header_name:
                headers.append(
                    (header_name, value.decode("latin-1").strip())
                )
        occurrences: dict[str, int] = {}
        for name, _value in headers:
            key = name.casefold()
            occurrences[key] = occurrences.get(key, 0) + 1
        duplicate_headers = {
            name: count for name, count in sorted(occurrences.items()) if count > 1
        }

        content_length_values: list[str] = []
        te_tokens: list[str] = []
        for name, value in headers:
            lowered = name.casefold()
            if lowered == "content-length":
                content_length_values.append(value)
            elif lowered == "transfer-encoding":
                te_tokens.extend(
                    token.strip().casefold()
                    for token in value.split(",")
                    if token.strip()
                )

        candidates: list[str] = []
        chunks: list[dict[str, Any]] = []
        trailers: list[tuple[str, str]] = []
        anomalies: list[str] = []
        framing = "NONE"
        content_length: int | None = None
        end = body_start
        body = b""
        complete = True
        chunked = bool(te_tokens) and te_tokens[-1] == "chunked"
        has_cl = bool(content_length_values)

        if chunked:
            candidates.append("CHUNKED")
        if has_cl:
            candidates.append("CONTENT_LENGTH")
        if chunked and has_cl:
            anomalies.append("te_cl_coexist")

        use_chunked = chunked
        if chunked and has_cl and self.te_cl_policy == "content-length":
            use_chunked = False
            anomalies.append("server_chose_content_length")
        elif has_cl and not chunked:
            use_chunked = False
        elif not has_cl and not chunked:
            use_chunked = False

        if use_chunked:
            framing = "CHUNKED_WITH_CONTENT_LENGTH" if has_cl else "CHUNKED"
            cursor = body_start
            body_parts: list[bytes] = []
            while True:
                line_end = raw.find(b"\r\n", cursor)
                if line_end < 0:
                    line_end = raw.find(b"\n", cursor)
                    step = 1
                else:
                    step = 2
                if line_end < 0:
                    anomalies.append("chunk_size_incomplete")
                    complete = False
                    cursor = len(raw)
                    break
                line = raw[cursor:line_end]
                cursor = line_end + step
                match = re.match(rb"^\s*([0-9A-Fa-f]+)(?:\s*;(.*))?$", line)
                if match is None and self.chunk_policy == "lenient":
                    match = re.match(rb"^\s*([0-9A-Fa-f]+)", line)
                    if match is not None:
                        anomalies.append("chunk_size_leniently_truncated")
                if match is None:
                    anomalies.append("chunk_size_invalid")
                    chunks.append(
                        {
                            "index": len(chunks),
                            "size": None,
                            "raw": line.decode("latin-1", errors="replace"),
                            "offset": cursor,
                        }
                    )
                    complete = False
                    body_parts.append(raw[cursor:])
                    cursor = len(raw)
                    break
                size = int(match.group(1), 16)
                chunks.append(
                    {
                        "index": len(chunks),
                        "size": size,
                        "extension": (
                            match.group(2).decode("latin-1").strip()
                            if match.lastindex and match.lastindex >= 2 and match.group(2)
                            else ""
                        ),
                        "offset": cursor,
                    }
                )
                if size == 0:
                    cursor = _parse_trailers(raw, cursor, trailers)
                    if self.trailer_policy == "ignore":
                        if trailers:
                            anomalies.append("trailers_ignored")
                        trailers.clear()
                    break
                if cursor + size > len(raw):
                    anomalies.append("chunk_data_incomplete")
                    body_parts.append(raw[cursor:])
                    cursor = len(raw)
                    complete = False
                    break
                body_parts.append(raw[cursor : cursor + size])
                cursor += size
                if raw[cursor : cursor + 2] == b"\r\n":
                    cursor += 2
                elif raw[cursor : cursor + 1] == b"\n":
                    cursor += 1
                    anomalies.append("chunk_data_bare_lf")
                else:
                    anomalies.append("chunk_data_missing_crlf")
                    complete = False
                    break
            body = b"".join(body_parts)
            end = cursor
        else:
            parsed_cls: list[int] = []
            for value in content_length_values:
                for token in value.split(","):
                    token = token.strip()
                    if token.isdigit():
                        parsed_cls.append(int(token))
                    else:
                        anomalies.append("content_length_invalid")
            if parsed_cls:
                if self.duplicate_cl_policy == "last":
                    content_length = parsed_cls[-1]
                else:
                    content_length = parsed_cls[0]
                if len(set(parsed_cls)) > 1:
                    anomalies.append("content_length_conflict")
                available = raw[body_start:]
                body = available[:content_length]
                end = body_start + len(body)
                if len(body) < content_length:
                    anomalies.append("content_length_body_incomplete")
                    complete = False
                framing = "CONTENT_LENGTH" if len(set(parsed_cls)) == 1 else (
                    "CONTENT_LENGTH_CONFLICT"
                )
            else:
                framing = "NONE"
                body = b""
                end = body_start

        return {
            "start_line": start_line,
            "framing": framing,
            "framing_candidates": candidates,
            "content_length": content_length,
            "content_length_values": content_length_values,
            "transfer_encoding": te_tokens,
            "duplicate_headers": duplicate_headers,
            "body_length": len(body),
            "body_sha256": hashlib.sha256(body).hexdigest(),
            "chunks": chunks,
            "trailers": [list(item) for item in trailers],
            "request_boundary": [0, end],
            "next_boundary": end,
            "raw_length": len(raw),
            "complete": complete,
            "anomalies": sorted(set(anomalies)),
            "remaining_bytes": len(raw) - end,
        }


def _find_head_end(raw: bytes) -> tuple[int | None, int]:
    crlf = raw.find(b"\r\n\r\n")
    lf = raw.find(b"\n\n")
    if crlf < 0 and lf < 0:
        return None, 0
    if crlf >= 0 and (lf < 0 or crlf <= lf):
        return crlf, 4
    return lf, 2


def _parse_trailers(
    raw: bytes,
    cursor: int,
    trailers: list[tuple[str, str]],
) -> int:
    while cursor < len(raw):
        crlf = raw.find(b"\r\n", cursor)
        if crlf < 0:
            lf = raw.find(b"\n", cursor)
            if lf < 0:
                return len(raw)
            line = raw[cursor:lf]
            cursor = lf + 1
        else:
            line = raw[cursor:crlf]
            cursor = crlf + 2
        if not line:
            return cursor
        if b":" in line:
            name, value = line.split(b":", 1)
            trailers.append(
                (
                    name.decode("latin-1", errors="replace").strip(),
                    value.decode("latin-1", errors="replace").strip(),
                )
            )
    return cursor
