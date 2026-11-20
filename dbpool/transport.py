from __future__ import annotations

import socket
from dataclasses import dataclass, field
from typing import Protocol

from .errors import (
    E_BUILD_BUDGET_EXCEEDED,
    E_ENDPOINT_REFUSED,
    E_ENDPOINT_TIMEOUT,
    E_MALFORMED_PROBE,
    PoolError,
)
from .model import Endpoint


PROBE_PREFIX = "POOL_PROBE_V1|"


class ConnectionHandle(Protocol):
    def close(self) -> None:
        ...


@dataclass
class SocketHandle:
    sock: socket.socket

    def close(self) -> None:
        try:
            self.sock.close()
        except OSError:
            pass


class LoopbackTransport:
    """Real local TCP probe transport."""

    def open(self, endpoint: Endpoint, *, budget_ms: int) -> ConnectionHandle:
        if endpoint.build_latency_ms > budget_ms:
            raise PoolError(
                E_BUILD_BUDGET_EXCEEDED,
                "endpoint build latency exceeds the per-round logical budget",
                operation="acquire",
                endpoint=endpoint.name,
                build_latency_ms=endpoint.build_latency_ms,
                round_budget_ms=budget_ms,
            )
        payload = f"{PROBE_PREFIX}{endpoint.name}\n".encode("utf-8")
        try:
            sock = socket.create_connection(
                (endpoint.host, endpoint.port),
                timeout=2.0,
            )
        except ConnectionRefusedError as exc:
            raise PoolError(
                E_ENDPOINT_REFUSED,
                "endpoint refused the connection",
                operation="acquire",
                endpoint=endpoint.name,
                address=endpoint.address,
            ) from exc
        except (socket.timeout, TimeoutError) as exc:
            raise PoolError(
                E_ENDPOINT_TIMEOUT,
                "endpoint connection timed out",
                operation="acquire",
                endpoint=endpoint.name,
                address=endpoint.address,
            ) from exc
        except OSError as exc:
            raise PoolError(
                E_ENDPOINT_REFUSED,
                "endpoint is unreachable",
                operation="acquire",
                endpoint=endpoint.name,
                address=endpoint.address,
                reason=str(exc),
            ) from exc
        try:
            sock.settimeout(2.0)
            sock.sendall(payload)
            received = _recv_line(sock, maximum=4096)
        except (socket.timeout, TimeoutError) as exc:
            sock.close()
            raise PoolError(
                E_ENDPOINT_TIMEOUT,
                "probe response timed out",
                operation="acquire",
                endpoint=endpoint.name,
            ) from exc
        except OSError as exc:
            sock.close()
            raise PoolError(
                E_MALFORMED_PROBE,
                "probe response could not be read",
                operation="acquire",
                endpoint=endpoint.name,
                reason=str(exc),
            ) from exc
        if received != payload:
            sock.close()
            raise PoolError(
                E_MALFORMED_PROBE,
                "probe response was malformed",
                operation="acquire",
                endpoint=endpoint.name,
                expected=payload.decode("utf-8", errors="replace").rstrip("\n"),
                actual=received.decode("utf-8", errors="replace").rstrip("\n"),
            )
        return SocketHandle(sock)


def _recv_line(sock: socket.socket, *, maximum: int) -> bytes:
    chunks = bytearray()
    while len(chunks) < maximum:
        chunk = sock.recv(1)
        if not chunk:
            break
        chunks.extend(chunk)
        if chunk == b"\n":
            break
    return bytes(chunks)


@dataclass
class SimulatedTransport:
    """Deterministic test transport with no network side effects."""

    refused: set[str] = field(default_factory=set)
    malformed: set[str] = field(default_factory=set)
    timeout: set[str] = field(default_factory=set)
    opened: list[str] = field(default_factory=list)
    closed: list[str] = field(default_factory=list)

    def open(self, endpoint: Endpoint, *, budget_ms: int) -> ConnectionHandle:
        if endpoint.build_latency_ms > budget_ms:
            raise PoolError(
                E_BUILD_BUDGET_EXCEEDED,
                "endpoint build latency exceeds the per-round logical budget",
                operation="acquire",
                endpoint=endpoint.name,
                build_latency_ms=endpoint.build_latency_ms,
                round_budget_ms=budget_ms,
            )
        if endpoint.name in self.refused:
            raise PoolError(
                E_ENDPOINT_REFUSED,
                "simulated endpoint refused the connection",
                operation="acquire",
                endpoint=endpoint.name,
            )
        if endpoint.name in self.timeout:
            raise PoolError(
                E_ENDPOINT_TIMEOUT,
                "simulated endpoint timed out",
                operation="acquire",
                endpoint=endpoint.name,
            )
        if endpoint.name in self.malformed:
            raise PoolError(
                E_MALFORMED_PROBE,
                "simulated endpoint returned a malformed probe response",
                operation="acquire",
                endpoint=endpoint.name,
            )
        self.opened.append(endpoint.name)
        owner = self

        class _Handle:
            def close(self) -> None:
                owner.closed.append(endpoint.name)

        return _Handle()
