"""Length-prefixed JSON frame protocol over loopback TCP.

Each request/response is one frame:
    magic(4 bytes = OSWB) | header_len(uint32 BE) | header JSON bytes |
    payload_len(uint64 BE) | payload bytes

The header is a JSON object; binary payloads (chunk/object bytes) travel
separately so the header stays small and deterministic.
"""

import json
import socket
import struct

MAGIC = b"OSWB"
_HEADER = struct.Struct(">4sI")
_PAYLOAD_LEN = struct.Struct(">Q")
MAX_HEADER_BYTES = 4 * 1024 * 1024
MAX_PAYLOAD_BYTES = 512 * 1024 * 1024


def send_frame(sock, header, payload=b""):
    if payload is None:
        payload = b""
    header_bytes = json.dumps(header, ensure_ascii=False, sort_keys=True).encode("utf-8")
    if len(header_bytes) > MAX_HEADER_BYTES:
        raise ValueError("frame header too large")
    if len(payload) > MAX_PAYLOAD_BYTES:
        raise ValueError("frame payload too large")
    sock.sendall(_HEADER.pack(MAGIC, len(header_bytes)) + header_bytes)
    sock.sendall(_PAYLOAD_LEN.pack(len(payload)) + payload)


def _recv_exact(sock, size):
    chunks = []
    remaining = size
    while remaining:
        block = sock.recv(min(remaining, 1024 * 1024))
        if not block:
            raise ConnectionError("connection closed while reading frame")
        chunks.append(block)
        remaining -= len(block)
    return b"".join(chunks)


def recv_frame(sock):
    prefix = _recv_exact(sock, _HEADER.size)
    magic, header_len = _HEADER.unpack(prefix)
    if magic != MAGIC:
        raise ConnectionError("bad frame magic")
    if header_len > MAX_HEADER_BYTES:
        raise ConnectionError("frame header too large")
    header = json.loads(_recv_exact(sock, header_len).decode("utf-8"))
    (payload_len,) = _PAYLOAD_LEN.unpack(_recv_exact(sock, _PAYLOAD_LEN.size))
    if payload_len > MAX_PAYLOAD_BYTES:
        raise ConnectionError("frame payload too large")
    payload = _recv_exact(sock, payload_len) if payload_len else b""
    return header, payload


def call(host, port, header, payload=b"", timeout=30.0):
    """Open a short-lived connection and exchange a single frame."""
    with socket.create_connection((host, port), timeout=timeout) as sock:
        sock.settimeout(timeout)
        send_frame(sock, header, payload)
        return recv_frame(sock)


def validate_port(port):
    if isinstance(port, bool) or not isinstance(port, int):
        raise ValueError("port must be an integer")
    if not 1 <= port <= 65535:
        raise ValueError("port must be within 1..65535")
    return port
