from __future__ import annotations

import json
import socket
import socketserver
import struct
import threading
from dataclasses import dataclass
from typing import Any

from .config import TunnelSpec, WorkbenchConfig
from .errors import BusinessError
from .framing import Reassembler, parse_fragment, tunnel_token
from .util import stable_json


class EchoHub:
    def __init__(self, tunnels: list[TunnelSpec] | None = None) -> None:
        self._lock = threading.Lock()
        self._streams: dict[str, Reassembler] = {}
        self._mtu_by_token: dict[bytes, int] = {}
        self._enforce_tokens = bool(tunnels)
        for tunnel in tunnels or []:
            self._mtu_by_token[tunnel_token(tunnel.name)] = tunnel.outer_mtu

    def handle_frame(self, frame: bytes) -> dict[str, Any]:
        try:
            parsed = parse_fragment(frame)
            expected_mtu = self._mtu_by_token.get(parsed.header.tunnel_token)
            if expected_mtu is None and self._enforce_tokens:
                raise BusinessError(
                    "E_TUNNEL_UNKNOWN",
                    "echo target does not know this tunnel token",
                    {"tunnel_token": parsed.header.tunnel_token.hex()},
                )
            if expected_mtu is not None and len(frame) > expected_mtu:
                raise BusinessError(
                    "E_MTU_EXCEEDED",
                    "fragment exceeds the tunnel outer MTU",
                    {"wire_length": len(frame), "outer_mtu": expected_mtu},
                )
            with self._lock:
                existing = self._streams.get(parsed.header.stream_key)
                if (
                    existing is not None
                    and existing.summary is not None
                    and parsed.header.sequence == 0
                ):
                    existing = None
                reassembler = existing or Reassembler()
                self._streams[parsed.header.stream_key] = reassembler
                duplicate = parsed.header.sequence in reassembler.fragments
                accepted = reassembler.add(frame, idempotent_duplicates=True)
                response = reassembler.response(accepted, duplicate=duplicate)
                response["profile_version"] = accepted.header.profile_version
                return response
        except BusinessError as exc:
            return {
                "ok": False,
                "ack": False,
                "error": exc.as_dict(),
            }
        except Exception as exc:  # defensive boundary for a network service
            return {
                "ok": False,
                "ack": False,
                "error": {
                    "code": "E_ECHO_INTERNAL",
                    "message": str(exc),
                    "details": {},
                },
            }

    def handle_json(self, body: bytes) -> dict[str, Any]:
        try:
            request = json.loads(body.decode("utf-8"))
            frame_hex = request["frame_hex"]
            frame = bytes.fromhex(frame_hex)
        except Exception:
            return {
                "ok": False,
                "ack": False,
                "error": {
                    "code": "E_ECHO_REQUEST",
                    "message": "request must contain frame_hex with a valid hex frame",
                    "details": {},
                },
            }
        return self.handle_frame(frame)


class _ThreadingUDPServer(socketserver.ThreadingUDPServer):
    allow_reuse_address = True
    daemon_threads = True


class _UDPHandler(socketserver.BaseRequestHandler):
    def handle(self) -> None:
        data, sock = self.request
        response = self.server.hub.handle_frame(data)  # type: ignore[attr-defined]
        sock.sendto(_json_bytes(response), self.client_address)


def _recv_exact(sock: socket.socket, count: int) -> bytes | None:
    chunks = bytearray()
    while len(chunks) < count:
        chunk = sock.recv(count - len(chunks))
        if not chunk:
            return None
        chunks.extend(chunk)
    return bytes(chunks)


class _TCPHandler(socketserver.BaseRequestHandler):
    def handle(self) -> None:
        request = self.request
        assert isinstance(request, socket.socket)
        try:
            request.settimeout(5.0)
            prefix = _recv_exact(request, 4)
            if prefix is None:
                return
            length = struct.unpack(">I", prefix)[0]
            if length > 1024 * 1024:
                response = {
                    "ok": False,
                    "ack": False,
                    "error": {
                        "code": "E_ECHO_REQUEST",
                        "message": "request is too large",
                        "details": {"length": length},
                    },
                }
            else:
                body = _recv_exact(request, length)
                if body is None:
                    return
                response = self.server.hub.handle_json(body)  # type: ignore[attr-defined]
            payload = _json_bytes(response)
            request.sendall(struct.pack(">I", len(payload)) + payload)
        except (OSError, ValueError):
            return


def _json_bytes(value: dict[str, Any]) -> bytes:
    return stable_json(value).encode("utf-8")


def _server_class(base: type[socketserver.BaseServer], host: str):
    family = socket.AF_INET6 if ":" in host else socket.AF_INET

    class FamilyServer(base):  # type: ignore[misc, valid-type]
        address_family = family
        allow_reuse_address = True
        daemon_threads = True

    return FamilyServer


@dataclass
class EchoEndpoint:
    transport: str
    host: str
    port: int


class EchoService:
    def __init__(self, endpoints: list[EchoEndpoint], hub: EchoHub) -> None:
        self.endpoints = endpoints
        self.hub = hub
        self._servers: list[socketserver.BaseServer] = []
        self._threads: list[threading.Thread] = []

    @classmethod
    def from_config(cls, config: WorkbenchConfig) -> "EchoService":
        endpoints = [
            EchoEndpoint(tunnel.transport, tunnel.listen_host, tunnel.listen_port)
            for tunnel in config.tunnels
        ]
        return cls(endpoints=endpoints, hub=EchoHub(list(config.tunnels)))

    def start(self) -> "EchoService":
        unique: set[tuple[str, str, int]] = set()
        for endpoint in self.endpoints:
            key = (endpoint.transport, endpoint.host, endpoint.port)
            if key in unique:
                continue
            unique.add(key)
            if endpoint.transport == "udp":
                server_cls = _server_class(_ThreadingUDPServer, endpoint.host)
                server = server_cls((endpoint.host, endpoint.port), _UDPHandler)
            elif endpoint.transport == "tcp":
                server_cls = _server_class(socketserver.ThreadingTCPServer, endpoint.host)
                server = server_cls((endpoint.host, endpoint.port), _TCPHandler)
            else:
                raise ValueError(f"unsupported echo transport {endpoint.transport}")
            server.hub = self.hub  # type: ignore[attr-defined]
            self._servers.append(server)
            thread = threading.Thread(
                target=server.serve_forever,
                name=f"echo-{endpoint.transport}-{endpoint.port}",
                daemon=True,
            )
            thread.start()
            self._threads.append(thread)
        return self

    def stop(self) -> None:
        for server in self._servers:
            server.shutdown()
            server.server_close()
        for thread in self._threads:
            thread.join(timeout=2.0)
        self._servers.clear()
        self._threads.clear()

    def __enter__(self) -> "EchoService":
        return self.start()

    def __exit__(self, exc_type, exc, tb) -> None:
        self.stop()

