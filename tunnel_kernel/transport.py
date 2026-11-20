"""Local UDP/TCP transports with real waits and cooperative cancellation."""

from __future__ import annotations

import json
import ipaddress
import select
import socket
import struct
import threading
import time
from typing import Any, Protocol

from .config import Peer, Policy
from .errors import E_ABORTED, E_IO, BusinessError


TCP_FRAME = struct.Struct("!I")
MAX_RESPONSE = 65_535


class Transport(Protocol):
    def send(self, datagram: bytes) -> dict[str, Any] | None:
        """Send one outer datagram and return its echo summary or ``None``."""

    def close(self) -> None:
        ...


def _abort_if_requested(abort_event: threading.Event | None) -> None:
    if abort_event is not None and abort_event.is_set():
        raise BusinessError(E_ABORTED, "forwarding was actively aborted")


def _remaining(deadline: float) -> float:
    return max(0.0, deadline - time.monotonic())


class UDPTransport:
    def __init__(
        self,
        host: str,
        port: int,
        *,
        timeout_ms: int,
        abort_event: threading.Event | None = None,
    ) -> None:
        self.host = host
        self.port = port
        self.timeout_ms = timeout_ms
        self.abort_event = abort_event
        self.family = _address_family(host)
        self.socket = socket.socket(self.family, socket.SOCK_DGRAM)
        self.socket.bind((host, 0))
        self.socket.setblocking(False)

    def send(self, datagram: bytes) -> dict[str, Any] | None:
        _abort_if_requested(self.abort_event)
        try:
            self.socket.sendto(datagram, (self.host, self.port))
        except OSError as exc:
            raise BusinessError(E_IO, f"UDP send failed: {exc}") from exc
        deadline = time.monotonic() + self.timeout_ms / 1000.0
        while True:
            _abort_if_requested(self.abort_event)
            remaining = _remaining(deadline)
            if remaining <= 0:
                return None
            readable, _, _ = select.select(
                [self.socket], [], [], min(remaining, 0.02)
            )
            if not readable:
                continue
            try:
                raw, _ = self.socket.recvfrom(MAX_RESPONSE)
            except OSError as exc:
                raise BusinessError(E_IO, f"UDP receive failed: {exc}") from exc
            return _parse_response(raw)

    def close(self) -> None:
        self.socket.close()


class TCPTransport:
    def __init__(
        self,
        host: str,
        port: int,
        *,
        timeout_ms: int,
        abort_event: threading.Event | None = None,
    ) -> None:
        self.host = host
        self.port = port
        self.timeout_ms = timeout_ms
        self.abort_event = abort_event
        self.socket: socket.socket | None = None

    def _connection(self) -> socket.socket:
        if self.socket is not None:
            return self.socket
        connection = socket.socket(_address_family(self.host), socket.SOCK_STREAM)
        connection.setblocking(False)
        try:
            connection.connect_ex((self.host, self.port))
            _, writable, exceptional = select.select(
                [], [connection], [connection], self.timeout_ms / 1000.0
            )
            if not writable or exceptional:
                raise OSError("connect timeout")
            error = connection.getsockopt(socket.SOL_SOCKET, socket.SO_ERROR)
            if error != 0:
                raise OSError(error)
        except OSError as exc:
            connection.close()
            raise BusinessError(E_IO, f"TCP connect failed: {exc}") from exc
        self.socket = connection
        return connection

    def send(self, datagram: bytes) -> dict[str, Any] | None:
        _abort_if_requested(self.abort_event)
        connection = self._connection()
        frame = TCP_FRAME.pack(len(datagram)) + datagram
        if not self._send_all(connection, frame):
            self._drop_connection()
            return None
        response = self._recv_frame(connection)
        if response is None:
            self._drop_connection()
            return None
        return _parse_response(response)

    def _send_all(self, connection: socket.socket, data: bytes) -> bool:
        deadline = time.monotonic() + self.timeout_ms / 1000.0
        offset = 0
        while offset < len(data):
            _abort_if_requested(self.abort_event)
            remaining = _remaining(deadline)
            if remaining <= 0:
                return False
            _, writable, exceptional = select.select(
                [], [connection], [connection], min(remaining, 0.02)
            )
            if exceptional:
                return False
            if not writable:
                continue
            try:
                sent = connection.send(data[offset:])
            except (BlockingIOError, InterruptedError):
                continue
            except OSError:
                return False
            if sent <= 0:
                return False
            offset += sent
        return True

    def _recv_frame(self, connection: socket.socket) -> bytes | None:
        header = self._recv_exact(connection, TCP_FRAME.size)
        if header is None:
            return None
        (length,) = TCP_FRAME.unpack(header)
        if length > MAX_RESPONSE:
            return None
        return self._recv_exact(connection, length)

    def _recv_exact(
        self, connection: socket.socket, length: int
    ) -> bytes | None:
        chunks: list[bytes] = []
        remaining_bytes = length
        deadline = time.monotonic() + self.timeout_ms / 1000.0
        while remaining_bytes > 0:
            _abort_if_requested(self.abort_event)
            remaining = _remaining(deadline)
            if remaining <= 0:
                return None
            readable, _, exceptional = select.select(
                [connection], [], [connection], min(remaining, 0.02)
            )
            if exceptional:
                return None
            if not readable:
                continue
            try:
                chunk = connection.recv(remaining_bytes)
            except (BlockingIOError, InterruptedError):
                continue
            except OSError:
                return None
            if not chunk:
                return None
            chunks.append(chunk)
            remaining_bytes -= len(chunk)
        return b"".join(chunks)

    def _drop_connection(self) -> None:
        if self.socket is not None:
            try:
                self.socket.close()
            finally:
                self.socket = None

    def close(self) -> None:
        self._drop_connection()


def _parse_response(raw: bytes) -> dict[str, Any]:
    try:
        value = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise BusinessError(E_IO, "echo service returned invalid JSON") from exc
    if not isinstance(value, dict):
        raise BusinessError(E_IO, "echo service response must be an object")
    return value


def _address_family(host: str) -> socket.AddressFamily:
    try:
        address = ipaddress.ip_address(host)
    except ValueError as exc:
        raise BusinessError(
            E_IO,
            "transport host must be a literal IP address",
            details={"host": host},
        ) from exc
    return socket.AF_INET6 if address.version == 6 else socket.AF_INET


def make_transport(
    peer: Peer,
    policy: Policy,
    *,
    abort_event: threading.Event | None = None,
) -> Transport:
    if peer.protocol == "udp":
        return UDPTransport(
            peer.host,
            peer.port,
            timeout_ms=policy.io_timeout_ms,
            abort_event=abort_event,
        )
    if peer.protocol == "tcp":
        return TCPTransport(
            peer.host,
            peer.port,
            timeout_ms=policy.io_timeout_ms,
            abort_event=abort_event,
        )
    raise BusinessError(
        E_IO,
        "unsupported peer protocol",
        details={"protocol": peer.protocol},
    )


def query_echo(
    peer: Peer,
    policy: Policy,
    *,
    message_id: str,
    abort_event: threading.Event | None = None,
) -> dict[str, Any] | None:
    transport = make_transport(peer, policy, abort_event=abort_event)
    try:
        body = json.dumps(
            {"message_id": message_id, "op": "query"},
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
        return transport.send(body)
    finally:
        transport.close()
