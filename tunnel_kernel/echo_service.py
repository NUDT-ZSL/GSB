"""Local loopback UDP/TCP receiver with fragment reassembly summaries."""

from __future__ import annotations

import argparse
import ipaddress
import json
import signal
import socket
import struct
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .codec import Reassembler, decode_outer
from .errors import E_BAD_ENDPOINT, E_BAD_FRAGMENT, BusinessError


TCP_FRAME = struct.Struct("!I")
MAX_FRAME = 65_535


def _json_bytes(payload: dict[str, Any]) -> bytes:
    return json.dumps(
        payload,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")


def _parse_json_object(raw: bytes) -> dict[str, Any] | None:
    if not raw.lstrip().startswith(b"{"):
        return None
    try:
        value = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None
    return value if isinstance(value, dict) else None


@dataclass(slots=True)
class MessageState:
    message_id: str
    reassembler: Reassembler = field(
        default_factory=lambda: Reassembler(tolerate_duplicates=True)
    )
    payload: bytes | None = None
    digest: str | None = None
    payload_len: int | None = None
    fragment_count: int | None = None

    def response(self, *, extra: dict[str, Any] | None = None) -> dict[str, Any]:
        summary = self.reassembler.summary()
        result: dict[str, Any] = {
            "code": "OK",
            "complete": bool(summary["complete"]),
            "confirmed_sequences": sorted(self.reassembler.fragments),
            "duplicates": summary["duplicates"],
            "message_id": self.message_id,
            "missing": summary["missing"],
            "out_of_order": summary["out_of_order"],
            "received": summary["received"],
        }
        if self.digest is not None:
            result["digest"] = self.digest
            result["payload_len"] = self.payload_len
            result["fragment_count"] = self.fragment_count
        if extra:
            result.update(extra)
        return result


class EchoReceiver:
    """In-memory receiver state intentionally local to one echo process."""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._messages: dict[str, MessageState] = {}
        self._datagrams_seen = 0
        self._datagrams_dropped = 0
        self._drop_remaining = 0

    def set_drop_first_datagrams(self, count: int) -> None:
        with self._lock:
            self._drop_remaining = max(0, count)

    def handle(self, raw: bytes) -> dict[str, Any]:
        with self._lock:
            control = _parse_json_object(raw)
            if control is not None:
                return self._control(control)

            self._datagrams_seen += 1
            if self._drop_remaining > 0:
                self._drop_remaining -= 1
                self._datagrams_dropped += 1
                return {"code": "DROP", "dropped": True}

            try:
                _, packets = decode_outer(raw)
                responses: list[dict[str, Any]] = []
                for packet in packets:
                    state = self._messages.get(packet.message_id)
                    if state is None:
                        state = MessageState(
                            message_id=packet.message_id,
                            reassembler=Reassembler(
                                message_id=packet.message_id,
                                tolerate_duplicates=True,
                            ),
                        )
                        self._messages[packet.message_id] = state
                    if state.digest is not None:
                        state.reassembler.add(packet)
                        responses.append(state.response())
                        continue
                    state.reassembler.add(packet)
                    if state.reassembler.complete:
                        result = state.reassembler.result()
                        state.payload = result.payload
                        state.digest = result.digest
                        state.payload_len = len(result.payload)
                        state.fragment_count = result.fragment_count
                    responses.append(state.response())
                if len(responses) == 1:
                    return responses[0]
                merged = responses[-1]
                merged["batch_messages"] = len(responses)
                return merged
            except BusinessError as exc:
                return exc.as_dict()

    def _control(self, control: dict[str, Any]) -> dict[str, Any]:
        operation = control.get("op")
        if operation == "ping":
            return {
                "code": "OK",
                "datagrams_dropped": self._datagrams_dropped,
                "datagrams_seen": self._datagrams_seen,
                "messages": len(self._messages),
                "op": "pong",
            }
        if operation == "query":
            message_id = control.get("message_id")
            if not isinstance(message_id, str) or message_id not in self._messages:
                return {
                    "code": "E_NOT_FOUND",
                    "message": "message was not received by this echo process",
                    "message_id": message_id,
                }
            return self._messages[message_id].response()
        return {
            "code": E_BAD_FRAGMENT,
            "message": "unknown control operation",
            "op": operation,
        }

    def dump_state(self) -> dict[str, Any]:
        with self._lock:
            return {
                "messages": {
                    message_id: state.response()
                    for message_id, state in sorted(self._messages.items())
                }
            }


class LoopbackEchoService:
    """Threaded UDP and TCP echo service bound only to a loopback address."""

    def __init__(
        self,
        *,
        host: str = "127.0.0.1",
        udp_port: int = 39001,
        tcp_port: int = 39002,
        drop_first_datagrams: int = 0,
    ) -> None:
        try:
            address = ipaddress.ip_address(host)
        except ValueError as exc:
            raise BusinessError(
                E_BAD_ENDPOINT, "echo host must be a literal IP address"
            ) from exc
        if not address.is_loopback:
            raise BusinessError(E_BAD_ENDPOINT, "echo host must be loopback")
        self.host = host
        self.address_family = socket.AF_INET6 if address.version == 6 else socket.AF_INET
        self.udp_port = udp_port
        self.tcp_port = tcp_port
        self.receiver = EchoReceiver()
        self.receiver.set_drop_first_datagrams(drop_first_datagrams)
        self._stop = threading.Event()
        self._udp_socket: socket.socket | None = None
        self._tcp_socket: socket.socket | None = None
        self._threads: list[threading.Thread] = []
        self.bound_udp_port: int | None = None
        self.bound_tcp_port: int | None = None

    def start(self) -> dict[str, int]:
        udp_socket = socket.socket(self.address_family, socket.SOCK_DGRAM)
        udp_socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        udp_socket.bind((self.host, self.udp_port))
        udp_socket.settimeout(0.1)
        self._udp_socket = udp_socket
        self.bound_udp_port = int(udp_socket.getsockname()[1])

        tcp_socket = socket.socket(self.address_family, socket.SOCK_STREAM)
        tcp_socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        tcp_socket.bind((self.host, self.tcp_port))
        tcp_socket.listen(16)
        tcp_socket.settimeout(0.1)
        self._tcp_socket = tcp_socket
        self.bound_tcp_port = int(tcp_socket.getsockname()[1])

        udp_thread = threading.Thread(
            target=self._udp_loop,
            name="tunnel-echo-udp",
            daemon=True,
        )
        tcp_thread = threading.Thread(
            target=self._tcp_loop,
            name="tunnel-echo-tcp",
            daemon=True,
        )
        self._threads = [udp_thread, tcp_thread]
        for thread in self._threads:
            thread.start()
        return {
            "tcp_port": self.bound_tcp_port,
            "udp_port": self.bound_udp_port,
        }

    def stop(self) -> None:
        self._stop.set()
        for sock in (self._udp_socket, self._tcp_socket):
            if sock is not None:
                try:
                    sock.close()
                except OSError:
                    pass
        for thread in self._threads:
            thread.join(timeout=1.0)

    def _udp_loop(self) -> None:
        assert self._udp_socket is not None
        while not self._stop.is_set():
            try:
                raw, address = self._udp_socket.recvfrom(MAX_FRAME)
            except socket.timeout:
                continue
            except OSError:
                break
            response = self.receiver.handle(raw)
            if response.get("code") == "DROP":
                continue
            try:
                self._udp_socket.sendto(_json_bytes(response), address)
            except OSError:
                pass

    def _tcp_loop(self) -> None:
        assert self._tcp_socket is not None
        while not self._stop.is_set():
            try:
                connection, _ = self._tcp_socket.accept()
            except socket.timeout:
                continue
            except OSError:
                break
            thread = threading.Thread(
                target=self._handle_tcp_connection,
                args=(connection,),
                name="tunnel-echo-tcp-client",
                daemon=True,
            )
            thread.start()

    def _handle_tcp_connection(self, connection: socket.socket) -> None:
        with connection:
            connection.settimeout(0.5)
            while not self._stop.is_set():
                try:
                    header = _recv_exact(connection, TCP_FRAME.size)
                    if header is None:
                        break
                    (length,) = TCP_FRAME.unpack(header)
                    raw = _recv_exact(connection, length)
                    if raw is None:
                        break
                except (OSError, ValueError):
                    break
                response = self.receiver.handle(raw)
                if response.get("code") == "DROP":
                    continue
                body = _json_bytes(response)
                try:
                    connection.sendall(TCP_FRAME.pack(len(body)) + body)
                except OSError:
                    break


def _recv_exact(connection: socket.socket, length: int) -> bytes | None:
    chunks: list[bytes] = []
    remaining = length
    while remaining > 0:
        chunk = connection.recv(remaining)
        if not chunk:
            return None
        chunks.append(chunk)
        remaining -= len(chunk)
    return b"".join(chunks)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Local loopback UDP/TCP echo service")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--udp-port", type=int, default=39001)
    parser.add_argument("--tcp-port", type=int, default=39002)
    parser.add_argument("--drop-first-datagrams", type=int, default=0)
    parser.add_argument("--ready-file", type=Path)
    args = parser.parse_args(argv)

    service = LoopbackEchoService(
        host=args.host,
        udp_port=args.udp_port,
        tcp_port=args.tcp_port,
        drop_first_datagrams=args.drop_first_datagrams,
    )
    ports = service.start()
    ready = {"code": "OK", "host": args.host, **ports}
    rendered = json.dumps(ready, sort_keys=True)
    if args.ready_file:
        args.ready_file.write_text(rendered + "\n", encoding="utf-8")
    print(rendered, flush=True)

    stop = threading.Event()

    def _request_stop(_signum: int, _frame: Any) -> None:
        stop.set()

    signal.signal(signal.SIGINT, _request_stop)
    signal.signal(signal.SIGTERM, _request_stop)
    try:
        stop.wait()
    finally:
        service.stop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
