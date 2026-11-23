from __future__ import annotations

import hashlib
import struct
from dataclasses import dataclass
from typing import Any

from .errors import fail
from .util import sha256_hex

MAGIC = b"TWB1"
PROTOCOL_VERSION = 1
FLAG_END = 0x01
HEADER_STRUCT = struct.Struct(">4sBBH8s8sIIIIII32s")
HEADER_SIZE = HEADER_STRUCT.size
MAX_FRAME_SIZE = 65535


def tunnel_token(tunnel: str) -> bytes:
    return hashlib.sha256(b"TWB-TUNNEL\0" + tunnel.encode("utf-8")).digest()[:8]


def stream_token(
    tunnel: str,
    payload_digest: str,
    initial_mtu: int,
    threshold: int,
    overhead: int,
    initial_profile_version: int,
    run_id: str,
) -> bytes:
    material = (
        f"TWB-STREAM\0{tunnel}\0{payload_digest}\0{initial_mtu}\0"
        f"{threshold}\0{overhead}\0{initial_profile_version}\0{run_id}"
    ).encode("utf-8")
    return hashlib.sha256(material).digest()[:8]


@dataclass(frozen=True)
class FragmentHeader:
    flags: int
    tunnel_token: bytes
    stream_token: bytes
    sequence: int
    payload_length: int
    total_length: int
    overhead: int
    profile_version: int
    reserved: int
    total_digest: bytes

    @property
    def is_last(self) -> bool:
        return bool(self.flags & FLAG_END)

    @property
    def stream_key(self) -> str:
        return f"{self.tunnel_token.hex()}:{self.stream_token.hex()}"


@dataclass(frozen=True)
class ParsedFragment:
    frame: bytes
    header_bytes: bytes
    header: FragmentHeader
    seal: bytes
    payload: bytes
    header_digest: str
    fragment_digest: str


@dataclass(frozen=True)
class ReassemblySummary:
    stream_key: str
    fragment_count: int
    total_length: int
    digest: str
    payload: bytes


def encode_header(header: FragmentHeader) -> bytes:
    if header.reserved != 0:
        fail("E_FRAGMENT_HEADER_INVALID", "reserved header field must be zero")
    return HEADER_STRUCT.pack(
        MAGIC,
        PROTOCOL_VERSION,
        header.flags,
        HEADER_SIZE,
        header.tunnel_token,
        header.stream_token,
        header.sequence,
        header.payload_length,
        header.total_length,
        header.overhead,
        header.profile_version,
        header.reserved,
        header.total_digest,
    )


def decode_header(data: bytes) -> tuple[FragmentHeader, bytes]:
    if len(data) < HEADER_SIZE:
        fail(
            "E_FRAGMENT_HEADER_INVALID",
            "fragment is shorter than the fixed header",
            details={"length": len(data), "header_size": HEADER_SIZE},
        )
    (
        magic,
        version,
        flags,
        header_length,
        tunnel_token_value,
        stream_token_value,
        sequence,
        payload_length,
        total_length,
        overhead,
        profile_version,
        reserved,
        total_digest,
    ) = HEADER_STRUCT.unpack(data[:HEADER_SIZE])
    if magic != MAGIC:
        fail("E_FRAGMENT_MAGIC", "fragment magic does not match TWB1")
    if version != PROTOCOL_VERSION:
        fail(
            "E_FRAGMENT_VERSION",
            "unsupported fragment protocol version",
            details={"version": version},
        )
    if header_length != HEADER_SIZE:
        fail(
            "E_FRAGMENT_HEADER_INVALID",
            "header length is inconsistent with this protocol",
            details={"header_length": header_length, "expected": HEADER_SIZE},
        )
    if flags & ~FLAG_END:
        fail("E_FRAGMENT_FLAGS", "fragment contains unknown flags", details={"flags": flags})
    if reserved != 0:
        fail("E_FRAGMENT_HEADER_INVALID", "reserved header field must be zero")
    if payload_length <= 0:
        fail("E_FRAGMENT_LENGTH", "fragment payload length must be positive")
    if total_length <= 0 or payload_length > total_length:
        fail(
            "E_FRAGMENT_LENGTH",
            "fragment length exceeds the total payload length",
            details={
                "payload_length": payload_length,
                "total_length": total_length,
            },
        )
    if profile_version <= 0:
        fail("E_PROFILE_VERSION_INVALID", "fragment profile version must be positive")
    header = FragmentHeader(
        flags=flags,
        tunnel_token=tunnel_token_value,
        stream_token=stream_token_value,
        sequence=sequence,
        payload_length=payload_length,
        total_length=total_length,
        overhead=overhead,
        profile_version=profile_version,
        reserved=reserved,
        total_digest=total_digest,
    )
    return header, data[HEADER_SIZE:]


def parse_fragment(frame: bytes, expected_mtu: int | None = None) -> ParsedFragment:
    if len(frame) > MAX_FRAME_SIZE:
        fail(
            "E_FRAGMENT_LENGTH",
            "fragment exceeds the transport maximum",
            details={"length": len(frame), "maximum": MAX_FRAME_SIZE},
        )
    header, tail = decode_header(frame)
    expected_length = header.overhead + header.payload_length
    if len(tail) != expected_length:
        fail(
            "E_FRAGMENT_LENGTH",
            "fragment wire length does not match its header",
            details={
                "wire_length": len(frame),
                "expected": HEADER_SIZE + expected_length,
            },
        )
    if expected_mtu is not None and len(frame) > expected_mtu:
        fail(
            "E_MTU_EXCEEDED",
            "fragment exceeds the configured outer MTU",
            details={"wire_length": len(frame), "outer_mtu": expected_mtu},
        )
    seal = tail[: header.overhead]
    payload = tail[header.overhead :]
    header_bytes = frame[:HEADER_SIZE]
    return ParsedFragment(
        frame=frame,
        header_bytes=header_bytes,
        header=header,
        seal=seal,
        payload=payload,
        header_digest=sha256_hex(header_bytes),
        fragment_digest=sha256_hex(frame),
    )


def build_fragment(
    payload: bytes,
    *,
    offset: int,
    length: int,
    sequence: int,
    tunnel: str,
    stream_token_value: bytes,
    total_digest: bytes,
    overhead: int,
    profile_version: int,
    outer_mtu: int,
) -> ParsedFragment:
    if length <= 0 or offset < 0 or offset + length > len(payload):
        fail(
            "E_FRAGMENT_RANGE",
            "fragment range is outside the payload",
            details={"offset": offset, "length": length, "payload_length": len(payload)},
        )
    end = offset + length == len(payload)
    header = FragmentHeader(
        flags=FLAG_END if end else 0,
        tunnel_token=tunnel_token(tunnel),
        stream_token=stream_token_value,
        sequence=sequence,
        payload_length=length,
        total_length=len(payload),
        overhead=overhead,
        profile_version=profile_version,
        reserved=0,
        total_digest=total_digest,
    )
    header_bytes = encode_header(header)
    frame = header_bytes + (b"\x00" * overhead) + payload[offset : offset + length]
    if len(frame) > outer_mtu:
        fail(
            "E_MTU_EXCEEDED",
            "constructed fragment exceeds the outer MTU",
            details={"wire_length": len(frame), "outer_mtu": outer_mtu},
        )
    return parse_fragment(frame, outer_mtu)


def reassemble(
    frames: list[bytes], *, expected_count: int | None = None
) -> ReassemblySummary:
    reassembler = Reassembler()
    for frame in frames:
        reassembler.add(frame)
    return reassembler.finalize(expected_count=expected_count)


class Reassembler:
    """One-stream strict reassembler.

    Strict mode reports duplicate fragments as a business error. The echo
    service can opt into idempotent duplicate handling because network retries
    must be safe.
    """

    def __init__(self) -> None:
        self.fragments: dict[int, ParsedFragment] = {}
        self.end_sequence: int | None = None
        self.identity: tuple[bytes, bytes, int, bytes] | None = None
        self.summary: ReassemblySummary | None = None

    def _check_identity(self, parsed: ParsedFragment) -> None:
        header = parsed.header
        identity = (
            header.tunnel_token,
            header.stream_token,
            header.total_length,
            header.total_digest,
        )
        if self.identity is None:
            self.identity = identity
        elif identity != self.identity:
            fail(
                "E_STREAM_CONFLICT",
                "fragment belongs to a conflicting reassembly stream",
                details={"stream_key": header.stream_key},
            )

    def add(self, frame: bytes, *, idempotent_duplicates: bool = False) -> ParsedFragment:
        parsed = parse_fragment(frame)
        self._check_identity(parsed)
        header = parsed.header
        if header.sequence in self.fragments:
            previous = self.fragments[header.sequence]
            if previous.fragment_digest == parsed.fragment_digest:
                if idempotent_duplicates:
                    return previous
                fail(
                    "E_DUPLICATE_FRAGMENT",
                    "fragment sequence was already received",
                    details={"sequence": header.sequence},
                )
            fail(
                "E_FRAGMENT_CONFLICT",
                "a different fragment already occupies this sequence",
                details={"sequence": header.sequence},
            )
        if self.end_sequence is not None and header.sequence > self.end_sequence:
            fail(
                "E_SEQUENCE_AFTER_END",
                "fragment sequence appears after the end fragment",
                details={"sequence": header.sequence, "end_sequence": self.end_sequence},
            )
        if header.is_last:
            if self.end_sequence is not None and self.end_sequence != header.sequence:
                fail(
                    "E_END_CONFLICT",
                    "multiple conflicting end fragments were received",
                    details={
                        "sequence": header.sequence,
                        "end_sequence": self.end_sequence,
                    },
                )
            self.end_sequence = header.sequence
        self.fragments[header.sequence] = parsed
        return parsed

    def is_complete(self) -> bool:
        if self.end_sequence is None:
            return False
        if len(self.fragments) != self.end_sequence + 1:
            return False
        return all(sequence in self.fragments for sequence in range(self.end_sequence + 1))

    def finalize(self, *, expected_count: int | None = None) -> ReassemblySummary:
        if self.identity is None:
            fail("E_NO_FRAGMENTS", "no fragments have been received")
        if self.end_sequence is None:
            fail("E_END_MISSING", "the end fragment has not been received")
        actual_count = self.end_sequence + 1
        if expected_count is not None and actual_count != expected_count:
            fail(
                "E_FRAGMENT_COUNT_MISMATCH",
                "end sequence does not match the expected fragment count",
                details={"expected": expected_count, "actual": actual_count},
            )
        missing = [
            sequence
            for sequence in range(self.end_sequence + 1)
            if sequence not in self.fragments
        ]
        if missing:
            fail(
                "E_MISSING_FRAGMENT",
                "reassembly is missing one or more fragments",
                details={"missing": missing},
            )
        chunks = [self.fragments[sequence].payload for sequence in range(actual_count)]
        payload = b"".join(chunks)
        tunnel_token_value, stream_token_value, total_length, total_digest = self.identity
        if len(payload) != total_length:
            fail(
                "E_LENGTH_MISMATCH",
                "reassembled payload length does not match the header",
                details={"expected": total_length, "actual": len(payload)},
            )
        digest = hashlib.sha256(payload).digest()
        if digest != total_digest:
            fail(
                "E_DIGEST_MISMATCH",
                "reassembled payload digest does not match the header",
                details={"expected": total_digest.hex(), "actual": digest.hex()},
            )
        summary = ReassemblySummary(
            stream_key=f"{tunnel_token_value.hex()}:{stream_token_value.hex()}",
            fragment_count=actual_count,
            total_length=total_length,
            digest=digest.hex(),
            payload=payload,
        )
        self.summary = summary
        return summary

    def response(self, parsed: ParsedFragment, *, duplicate: bool = False) -> dict[str, Any]:
        result: dict[str, Any] = {
            "ok": True,
            "ack": True,
            "sequence": parsed.header.sequence,
            "fragment_digest": parsed.fragment_digest,
            "header_digest": parsed.header_digest,
            "duplicate": duplicate,
            "complete": False,
            "summary": None,
        }
        if duplicate and self.summary is not None:
            result["complete"] = True
            result["summary"] = self._summary_dict(self.summary)
            return result
        if self.is_complete():
            try:
                summary = self.finalize()
            except Exception:
                raise
            result["complete"] = True
            result["summary"] = self._summary_dict(summary)
        return result

    @staticmethod
    def _summary_dict(summary: ReassemblySummary) -> dict[str, Any]:
        return {
            "stream_key": summary.stream_key,
            "fragment_count": summary.fragment_count,
            "total_length": summary.total_length,
            "digest": summary.digest,
            "status": "OK",
        }

