from __future__ import annotations

import hashlib
import json
import select
import socket
import struct
import time
from dataclasses import dataclass
from typing import Any, Callable

AbortProbe = Callable[[], bool]


@dataclass
class TransportResult:
    status: str
    response: dict[str, Any] | None = None
    error: str | None = None


class EchoTransport:
    def __init__(self, transport: str, host: str, port: int) -> None:
        self.transport = transport
        self.host = host
        self.port = port

    def send(
        self,
        frame: bytes,
        *,
        timeout_ms: float,
        abort_probe: AbortProbe,
    ) -> TransportResult:
        timeout_seconds = max(0.01, min(1.0, timeout_ms / 1000.0))
        if self.transport == "udp":
            return self._send_udp(frame, timeout_seconds, abort_probe)
        return self._send_tcp(frame, timeout_seconds, abort_probe)

    def wait_without_send(
        self, *, timeout_ms: float, abort_probe: AbortProbe
    ) -> TransportResult:
        timeout_seconds = max(0.01, min(1.0, timeout_ms / 1000.0))
        deadline = time.monotonic() + timeout_seconds
        while True:
            if abort_probe():
                return TransportResult("ABORTED")
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return TransportResult("TIMEOUT")
            time.sleep(min(0.02, remaining))

    def _send_udp(
        self,
        frame: bytes,
        timeout_seconds: float,
        abort_probe: AbortProbe,
    ) -> TransportResult:
        family = socket.AF_INET6 if ":" in self.host else socket.AF_INET
        sock = socket.socket(family, socket.SOCK_DGRAM)
        try:
            sock.setblocking(False)
            sock.sendto(frame, (self.host, self.port))
            deadline = time.monotonic() + timeout_seconds
            while True:
                if abort_probe():
                    return TransportResult("ABORTED")
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return TransportResult("TIMEOUT")
                readable, _, _ = select.select([sock], [], [], min(0.02, remaining))
                if not readable:
                    continue
                try:
                    body = sock.recv(1024 * 1024)
                except BlockingIOError:
                    continue
                return self._decode_response(body)
        except OSError as exc:
            return TransportResult("ERROR", error=str(exc))
        finally:
            sock.close()

    def _send_tcp(
        self,
        frame: bytes,
        timeout_seconds: float,
        abort_probe: AbortProbe,
    ) -> TransportResult:
        family = socket.AF_INET6 if ":" in self.host else socket.AF_INET
        sock = socket.socket(family, socket.SOCK_STREAM)
        try:
            sock.settimeout(timeout_seconds)
            sock.connect((self.host, self.port))
            body = json.dumps(
                {"frame_hex": frame.hex()},
                separators=(",", ":"),
                sort_keys=True,
            ).encode("utf-8")
            sock.sendall(struct.pack(">I", len(body)) + body)
            deadline = time.monotonic() + timeout_seconds
            header = bytearray()
            while len(header) < 4:
                if abort_probe():
                    return TransportResult("ABORTED")
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return TransportResult("TIMEOUT")
                sock.settimeout(min(0.05, remaining))
                try:
                    chunk = sock.recv(4 - len(header))
                except socket.timeout:
                    continue
                if not chunk:
                    return TransportResult("ERROR", error="echo closed the TCP stream")
                header.extend(chunk)
            length = struct.unpack(">I", header)[0]
            if length > 1024 * 1024:
                return TransportResult("ERROR", error="echo response is too large")
            body = bytearray()
            while len(body) < length:
                if abort_probe():
                    return TransportResult("ABORTED")
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return TransportResult("TIMEOUT")
                sock.settimeout(min(0.05, remaining))
                try:
                    chunk = sock.recv(length - len(body))
                except socket.timeout:
                    continue
                if not chunk:
                    return TransportResult("ERROR", error="echo closed the TCP stream")
                body.extend(chunk)
            return self._decode_response(bytes(body))
        except (OSError, socket.timeout) as exc:
            return TransportResult("ERROR", error=str(exc))
        finally:
            sock.close()

    @staticmethod
    def _decode_response(body: bytes) -> TransportResult:
        try:
            response = json.loads(body.decode("utf-8"))
        except Exception as exc:
            return TransportResult("ERROR", error=f"invalid echo JSON: {exc}")
        if not isinstance(response, dict):
            return TransportResult("ERROR", error="echo response must be an object")
        if not response.get("ok", False):
            error = response.get("error", {})
            code = error.get("code", "E_ECHO_FAILURE")
            return TransportResult("ERROR", response=response, error=code)
        return TransportResult("ACK", response=response)


class DeterministicLink:
    """Deterministic impairment model for local loopback samples."""

    def should_drop(
        self,
        *,
        run_id: str,
        profile_version: int,
        sequence: int,
        generation: int,
        loss_rate: float,
    ) -> bool:
        if loss_rate <= 0.0:
            return False
        if loss_rate >= 1.0:
            return True
        material = (
            f"TWB-LINK-DROP\0{run_id}\0{profile_version}\0{sequence}\0{generation}"
        ).encode("utf-8")
        value = int.from_bytes(hashlib.sha256(material).digest()[:8], "big")
        return (value / float(1 << 64)) < loss_rate

