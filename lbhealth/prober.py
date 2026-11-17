"""Real loopback TCP/HTTP health probes with socket-derived timeouts."""

from __future__ import annotations

import errno
import socket
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Iterable

from .config import Config, MemberConfig
from .models import ProbeResult


def _mono_ms() -> int:
    return time.monotonic_ns() // 1_000_000


def _clean_summary(value: str, limit: int = 160) -> str:
    compact = " ".join(value.replace("\r", " ").replace("\n", " ").split())
    return compact[:limit]


def _failure_summary(exc: BaseException) -> tuple[str, str]:
    if isinstance(exc, (TimeoutError, socket.timeout)):
        return "socket timeout", "timeout"
    if isinstance(exc, ConnectionRefusedError):
        return "connection refused", "refused"
    if isinstance(exc, ConnectionResetError):
        return "connection reset", "reset"
    if isinstance(exc, OSError) and exc.errno in {errno.ECONNREFUSED, 10061}:
        return "connection refused", "refused"
    if isinstance(exc, OSError) and exc.errno in {errno.ETIMEDOUT, 10060}:
        return "socket timeout", "timeout"
    return f"connection or protocol error ({type(exc).__name__})", "network_error"


class LoopbackProber:
    def __init__(self, config: Config) -> None:
        self.config = config
        self.timeout_s = config.pool.timeout_ms / 1000.0

    def probe_all(self, round_no: int) -> list[ProbeResult]:
        members = list(self.config.members)
        with ThreadPoolExecutor(max_workers=len(members), thread_name_prefix="health-probe") as pool:
            futures = [
                pool.submit(self.probe_member, member, round_no)
                for member in members
            ]
            # Preserve configuration order independent of completion order.
            return [future.result() for future in futures]

    def probe_member(self, member: MemberConfig, round_no: int) -> ProbeResult:
        started = _mono_ms()
        try:
            if member.protocol == "http":
                ok, summary, failure_code = self._probe_http(member)
            elif member.protocol == "tcp":
                ok, summary, failure_code = self._probe_tcp(member)
            else:  # Defensive; configuration validation prevents this.
                ok, summary, failure_code = False, "unsupported protocol", "protocol_error"
        except BaseException as exc:  # A probe must never crash the round.
            summary, failure_code = _failure_summary(exc)
            ok = False
        ended = _mono_ms()
        return ProbeResult(
            round_no=round_no,
            member_id=member.id,
            protocol=member.protocol,
            ok=ok,
            start_mono_ms=started,
            end_mono_ms=ended,
            summary=_clean_summary(summary),
            failure_code=failure_code,
        )

    def _probe_http(self, member: MemberConfig) -> tuple[bool, str, str | None]:
        request = (
            f"GET {member.probe_path} HTTP/1.1\r\n"
            f"Host: {member.endpoint()}\r\n"
            "Connection: close\r\n"
            "User-Agent: lbhealth/1.0\r\n"
            "\r\n"
        ).encode("ascii")
        with socket.create_connection((member.address, member.port), timeout=self.timeout_s) as sock:
            sock.settimeout(self.timeout_s)
            sock.sendall(request)
            response = self._read_http_head(sock)
        if not response:
            return False, "empty HTTP response", "empty_response"
        text = response.decode("iso-8859-1", errors="replace")
        first_line = text.split("\r\n", 1)[0].strip()
        parts = first_line.split()
        if len(parts) < 2 or not parts[0].startswith("HTTP/"):
            return False, f"malformed HTTP status line: {first_line}", "protocol_error"
        try:
            status = int(parts[1])
        except ValueError:
            return False, f"malformed HTTP status line: {first_line}", "protocol_error"
        headers, _, body = text.partition("\r\n\r\n")
        body_summary = _clean_summary(body, 48)
        summary = f"HTTP {status}"
        if body_summary:
            summary += f"; {body_summary}"
        ok = 200 <= status < 400
        return ok, summary, None if ok else "http_status"

    def _read_http_head(self, sock: socket.socket) -> bytes:
        chunks: list[bytes] = []
        total = 0
        while total < 8192:
            chunk = sock.recv(min(2048, 8192 - total))
            if not chunk:
                break
            chunks.append(chunk)
            total += len(chunk)
            if b"\r\n\r\n" in b"".join(chunks):
                break
        return b"".join(chunks)

    def _probe_tcp(self, member: MemberConfig) -> tuple[bool, str, str | None]:
        with socket.create_connection((member.address, member.port), timeout=self.timeout_s) as sock:
            sock.settimeout(self.timeout_s)
            sock.sendall(b"PING\n")
            response = sock.recv(64)
        if response.startswith(b"PONG"):
            return True, "TCP PONG", None
        if not response:
            return False, "TCP empty response", "empty_response"
        return False, f"TCP unexpected response: {response[:32]!r}", "protocol_error"


def probe_with_fake_results(
    config: Config, round_no: int, outcomes: dict[str, bool]
) -> list[ProbeResult]:
    """Test helper that still produces complete ProbeResult records.

    It is intentionally not used by the runtime probe path.
    """

    now = _mono_ms()
    results: list[ProbeResult] = []
    for member in config.members:
        ok = bool(outcomes[member.id])
        results.append(
            ProbeResult(
                round_no=round_no,
                member_id=member.id,
                protocol=member.protocol,
                ok=ok,
                start_mono_ms=now,
                end_mono_ms=now,
                summary="test ok" if ok else "test failure",
                failure_code=None if ok else "test_failure",
            )
        )
    return results
