"""Length-prefixed message framing over TCP loopback sockets.

Wire format: 4-byte big-endian header length, 4-byte big-endian payload
length, JSON header, raw payload bytes.
"""

import json
import socket
import struct

from .errors import E_TIMEOUT, SimError

_PREFIX = struct.Struct(">II")


def send_message(sock, header, payload=b""):
    head = json.dumps(header, separators=(",", ":")).encode("utf-8")
    sock.sendall(_PREFIX.pack(len(head), len(payload)) + head + payload)


def _recv_exact(sock, nbytes):
    buf = bytearray()
    while len(buf) < nbytes:
        try:
            chunk = sock.recv(nbytes - len(buf))
        except socket.timeout:
            raise SimError(E_TIMEOUT, "timed out waiting for ring message")
        if not chunk:
            raise ConnectionError("ring connection closed by peer")
        buf.extend(chunk)
    return bytes(buf)


def recv_message(sock):
    prefix = _recv_exact(sock, _PREFIX.size)
    hlen, plen = _PREFIX.unpack(prefix)
    header = json.loads(_recv_exact(sock, hlen).decode("utf-8"))
    payload = _recv_exact(sock, plen) if plen else b""
    return header, payload
