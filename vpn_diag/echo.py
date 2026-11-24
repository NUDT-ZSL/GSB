"""Loopback UDP echo service and real timeout-aware echo client."""
from __future__ import annotations

import socket
import struct
import threading
import time
from dataclasses import dataclass
from typing import Any, Iterable, Optional

from .codes import E_ECHO_TIMEOUT, E_ECHO_UNREACHABLE

MAGIC = b"VD"
PROTOCOL_VERSION = 1
HEADER = struct.Struct("!2sBBHI")


def encode_packet(key: str, attempt: int, payload: bytes = b"", flags: int = 0) -> bytes:
    key_bytes = key.encode("utf-8")
    if len(key_bytes) > 65535:
        raise ValueError("fragment key is too long for UDP protocol")
    return HEADER.pack(MAGIC, PROTOCOL_VERSION, flags, len(key_bytes), attempt) + key_bytes + payload


def decode_packet(data: bytes) -> tuple[str, int, int, bytes]:
    if len(data) < HEADER.size:
        raise ValueError("short VPN diagnostic packet")
    magic, version, flags, key_length, attempt = HEADER.unpack_from(data, 0)
    if magic != MAGIC:
        raise ValueError("bad packet magic")
    if version != PROTOCOL_VERSION:
        raise ValueError("unsupported protocol version")
    key_end = HEADER.size + key_length
    if len(data) < key_end:
        raise ValueError("truncated key")
    key = data[HEADER.size:key_end].decode("utf-8")
    return key, attempt, flags, data[key_end:]


class EchoServer:
    """Daemon-thread UDP echo server bound to 127.0.0.1 by default."""

    def __init__(self, host: str = "127.0.0.1", port: int = 0):
        self.host = host
        self.requested_port = port
        self._socket = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self._socket.bind((host, port))
        self._socket.settimeout(0.1)
        self.port = self._socket.getsockname()[1]
        self.address = (self.host, self.port)
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._serve, name=f"vpn-echo-{self.port}", daemon=True)
        self.received = 0
        self._lock = threading.Lock()

    def __enter__(self) -> "EchoServer":
        self.start()
        return self

    def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        self.stop()

    def start(self) -> "EchoServer":
        self._thread.start()
        return self

    def stop(self) -> None:
        self._stop.set()
        try:
            self._socket.close()
        except OSError:
            pass
        if self._thread.is_alive():
            self._thread.join(timeout=1.0)

    def _serve(self) -> None:
        while not self._stop.is_set():
            try:
                data, address = self._socket.recvfrom(65535)
            except socket.timeout:
                continue
            except OSError:
                break
            try:
                self._socket.sendto(data, address)
                with self._lock:
                    self.received += 1
            except OSError:
                continue


@dataclass
class EchoRequest:
    key: str
    attempt: int = 1
    payload: bytes = b""


class UdpEchoTransport:
    def __init__(self, timeout_ms: int = 200):
        self.timeout_ms = timeout_ms

    def exchange_many(
        self,
        endpoint: tuple[str, int],
        requests: Iterable[EchoRequest | dict[str, Any]],
        timeout_ms: Optional[int] = None,
    ) -> dict[str, dict[str, Any]]:
        request_list: list[EchoRequest] = []
        for item in requests:
            if isinstance(item, EchoRequest):
                request_list.append(item)
            else:
                request_list.append(EchoRequest(
                    key=item["key"],
                    attempt=int(item.get("attempt", 1)),
                    payload=item.get("payload", b""),
                ))
        if not request_list:
            return {}

        timeout = max(1, int(timeout_ms or self.timeout_ms)) / 1000.0
        results: dict[str, dict[str, Any]] = {}
        pending: dict[tuple[str, int], EchoRequest] = {}
        send_perf: dict[str, float] = {}
        send_wall: dict[str, float] = {}
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        deadline = time.monotonic() + timeout
        hard_error: Optional[OSError] = None
        try:
            for request in request_list:
                results[request.key] = {
                    "acknowledged": False,
                    "observed": False,
                    "measured_rtt_ms": None,
                    "code": None,
                    "endpoint": f"{endpoint[0]}:{endpoint[1]}",
                    "attempt": request.attempt,
                }
                packet = encode_packet(request.key, request.attempt, request.payload)
                try:
                    sock.sendto(packet, endpoint)
                    send_perf[request.key] = time.perf_counter()
                    send_wall[request.key] = time.time()
                    pending[(request.key, request.attempt)] = request
                except OSError as exc:
                    hard_error = exc
                    results[request.key]["code"] = E_ECHO_UNREACHABLE
                    results[request.key]["error"] = str(exc)

            if not pending and hard_error is not None:
                remaining = deadline - time.monotonic()
                if remaining > 0:
                    time.sleep(remaining)

            while pending:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                sock.settimeout(min(0.05, remaining))
                try:
                    data, _source = sock.recvfrom(65535)
                except socket.timeout:
                    if time.monotonic() >= deadline:
                        break
                    continue
                except ConnectionResetError as exc:
                    # Windows can surface an ICMP port-unreachable as
                    # WSAECONNRESET. Preserve the real timeout deadline rather
                    # than turning the diagnostic round into an instant failure.
                    hard_error = exc
                    remaining = deadline - time.monotonic()
                    if remaining > 0:
                        time.sleep(min(0.01, remaining))
                    continue
                except OSError as exc:
                    hard_error = exc
                    remaining = deadline - time.monotonic()
                    if remaining > 0:
                        time.sleep(min(0.01, remaining))
                    if time.monotonic() >= deadline:
                        break
                    continue

                try:
                    key, attempt, _flags, _payload = decode_packet(data)
                except (ValueError, struct.error):
                    continue
                match = pending.pop((key, attempt), None)
                if match is None:
                    continue
                rtt_ms = (time.perf_counter() - send_perf[key]) * 1000.0
                wall_rtt_ms = (time.time() - send_wall[key]) * 1000.0
                results[key].update({
                    "acknowledged": True,
                    "observed": True,
                    "measured_rtt_ms": round(rtt_ms, 6),
                    "wall_rtt_ms": round(wall_rtt_ms, 6),
                    "measured_at_epoch_ms": round(time.time() * 1000.0, 3),
                    "code": None,
                })
        finally:
            sock.close()

        for (key, _attempt), request in list(pending.items()):
            result = results[key]
            if result.get("code") is None:
                result["code"] = E_ECHO_TIMEOUT
            if hard_error is not None and not result.get("error"):
                result["error"] = str(hard_error)
            result["acknowledged"] = False
        if results and not any(item["observed"] for item in results.values()):
            for item in results.values():
                if item.get("code") == E_ECHO_TIMEOUT:
                    item["code"] = E_ECHO_UNREACHABLE
        return results

    def exchange_one(self, endpoint: tuple[str, int], key: str, attempt: int = 1,
                     payload: bytes = b"", timeout_ms: Optional[int] = None) -> dict[str, Any]:
        return self.exchange_many(endpoint, [EchoRequest(key, attempt, payload)], timeout_ms).get(key, {})


def unused_udp_port(host: str = "127.0.0.1") -> int:
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.bind((host, 0))
        return int(sock.getsockname()[1])
    finally:
        sock.close()
