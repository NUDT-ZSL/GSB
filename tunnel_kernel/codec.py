"""Deterministic fragment codec, aggregation envelopes and reassembly."""

from __future__ import annotations

import hashlib
import struct
import zlib
from dataclasses import dataclass, field
from typing import Iterable, Sequence

from .errors import (
    E_BAD_FRAGMENT,
    E_BAD_MTU,
    E_CONFLICTING_FRAGMENT,
    E_DIGEST_MISMATCH,
    E_DUPLICATE_FRAGMENT,
    E_MISSING_FRAGMENT,
    E_OUT_OF_ORDER,
    BusinessError,
    fail,
)


MAGIC_FRAGMENT = b"TKF1"
MAGIC_AGGREGATE = b"TKA1"
VERSION = 1
HEADER_STRUCT = struct.Struct("!4sBBHIIII16s16sHHI")
HEADER_SIZE = HEADER_STRUCT.size
AGGREGATE_STRUCT = struct.Struct("!4sBBH")
AGGREGATE_HEADER_SIZE = AGGREGATE_STRUCT.size
ENTRY_LENGTH_STRUCT = struct.Struct("!H")

FLAG_FINAL = 0x01
FLAG_ENCRYPTED = 0x02


def sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def derive_message_id(tunnel_id: str, payload_digest: str) -> str:
    return hashlib.sha256(
        f"{tunnel_id}\x00{payload_digest}".encode("utf-8")
    ).hexdigest()[:32]


@dataclass(frozen=True, slots=True)
class FragmentPacket:
    sequence: int
    fragment_count: int
    payload: bytes
    original_len: int
    message_id: str
    payload_digest: str
    encrypted: bool
    final: bool
    crypto_overhead: int
    raw: bytes = field(repr=False, compare=False)

    @property
    def digest(self) -> str:
        return sha256_hex(self.raw)


def _fragment_header(
    *,
    sequence: int,
    fragment_count: int,
    payload_len: int,
    original_len: int,
    message_id: str,
    payload_digest: str,
    encrypted: bool,
    final: bool,
    crypto_overhead: int,
) -> bytes:
    flags = (FLAG_FINAL if final else 0) | (FLAG_ENCRYPTED if encrypted else 0)
    prefix = HEADER_STRUCT.pack(
        MAGIC_FRAGMENT,
        VERSION,
        flags,
        HEADER_SIZE,
        sequence,
        fragment_count,
        payload_len,
        original_len,
        bytes.fromhex(message_id),
        bytes.fromhex(payload_digest),
        crypto_overhead,
        0,
        0,
    )
    crc = zlib.crc32(prefix[:-4]) & 0xFFFFFFFF
    return prefix[:-4] + struct.pack("!I", crc)


def fragment_payload(
    payload: bytes,
    *,
    message_id: str,
    mtu: int,
    fragment_threshold_bytes: int,
    encrypted: bool,
    crypto_overhead_bytes: int,
    start_sequence: int = 0,
    include_crypto_placeholder: bool = True,
    original_len: int | None = None,
    original_digest: str | None = None,
) -> list[FragmentPacket]:
    """Split a payload into deterministic fragments.

    The packet budget is ``min(mtu, fragment_threshold_bytes)``. The per
    fragment capacity subtracts the fixed header and, for encrypted peers, the
    configured crypto placeholder bytes. Fragment bytes are entirely a pure
    function of the inputs.
    """

    if not payload:
        fail(E_BAD_FRAGMENT, "payload must not be empty")
    if not isinstance(mtu, int) or mtu <= 0:
        fail(E_BAD_MTU, "mtu must be a positive integer")
    if not isinstance(fragment_threshold_bytes, int) or fragment_threshold_bytes <= 0:
        fail(E_BAD_FRAGMENT, "fragment threshold must be a positive integer")
    if start_sequence < 0:
        fail(E_BAD_FRAGMENT, "start sequence must not be negative")
    if len(message_id) != 32:
        fail(E_BAD_FRAGMENT, "message_id must be 32 hexadecimal characters")
    try:
        message_id_bytes = bytes.fromhex(message_id)
    except ValueError as exc:
        raise BusinessError(E_BAD_FRAGMENT, "message_id is not hexadecimal") from exc
    if len(message_id_bytes) != 16:
        fail(E_BAD_FRAGMENT, "message_id must decode to 16 bytes")

    packet_budget = min(mtu, fragment_threshold_bytes)
    overhead = crypto_overhead_bytes if encrypted else 0
    capacity = packet_budget - HEADER_SIZE - overhead
    if capacity <= 0:
        fail(
            E_BAD_MTU,
            "mtu/threshold leaves no room for fragment payload",
            details={
                "capacity": capacity,
                "crypto_overhead": overhead,
                "header_size": HEADER_SIZE,
                "packet_budget": packet_budget,
            },
        )
    if len(payload) + HEADER_SIZE + overhead <= packet_budget:
        chunks = [payload]
    else:
        chunks = [payload[offset : offset + capacity] for offset in range(0, len(payload), capacity)]

    logical_original_len = len(payload) if original_len is None else original_len
    if logical_original_len <= 0:
        fail(E_BAD_FRAGMENT, "original payload length must be positive")
    if original_digest is None:
        full_digest = hashlib.sha256(payload).digest()
    else:
        try:
            full_digest = bytes.fromhex(original_digest)
        except ValueError as exc:
            raise BusinessError(
                E_BAD_FRAGMENT, "original digest is not hexadecimal"
            ) from exc
        if len(full_digest) != 32:
            fail(E_BAD_FRAGMENT, "original digest must be 32 bytes")
    header_digest = full_digest[:16].hex()
    total_count = start_sequence + len(chunks)
    packets: list[FragmentPacket] = []
    for index, chunk in enumerate(chunks):
        sequence = start_sequence + index
        final = index == len(chunks) - 1
        header = _fragment_header(
            sequence=sequence,
            fragment_count=total_count,
            payload_len=len(chunk),
            original_len=logical_original_len,
            message_id=message_id,
            payload_digest=header_digest,
            encrypted=encrypted,
            final=final,
            crypto_overhead=overhead,
        )
        placeholder = b"\x00" * overhead if include_crypto_placeholder else b""
        raw = header + placeholder + chunk
        packets.append(
            FragmentPacket(
                sequence=sequence,
                fragment_count=total_count,
                payload=chunk,
                original_len=logical_original_len,
                message_id=message_id,
                payload_digest=header_digest,
                encrypted=encrypted,
                final=final,
                crypto_overhead=overhead,
                raw=raw,
            )
        )
    return packets


def parse_fragment(raw: bytes) -> FragmentPacket:
    if len(raw) < HEADER_SIZE:
        fail(E_BAD_FRAGMENT, "fragment shorter than header")
    try:
        (
            magic,
            version,
            flags,
            header_len,
            sequence,
            fragment_count,
            payload_len,
            original_len,
            message_id_bytes,
            payload_digest_bytes,
            crypto_overhead,
            reserved,
            header_crc,
        ) = HEADER_STRUCT.unpack(raw[:HEADER_SIZE])
    except struct.error as exc:
        raise BusinessError(E_BAD_FRAGMENT, "malformed fragment header") from exc
    if magic != MAGIC_FRAGMENT:
        fail(E_BAD_FRAGMENT, "bad fragment magic")
    if version != VERSION:
        fail(E_BAD_FRAGMENT, "unsupported fragment version")
    if header_len != HEADER_SIZE:
        fail(E_BAD_FRAGMENT, "unsupported fragment header length")
    if reserved != 0:
        fail(E_BAD_FRAGMENT, "fragment reserved field must be zero")
    calculated_crc = zlib.crc32(raw[: HEADER_SIZE - 4]) & 0xFFFFFFFF
    if calculated_crc != header_crc:
        fail(E_BAD_FRAGMENT, "fragment header checksum mismatch")
    expected_len = HEADER_SIZE + crypto_overhead + payload_len
    if len(raw) != expected_len:
        fail(
            E_BAD_FRAGMENT,
            "fragment length does not match header",
            details={"actual": len(raw), "expected": expected_len},
        )
    if fragment_count <= sequence:
        fail(E_BAD_FRAGMENT, "fragment count does not include sequence")
    body = raw[HEADER_SIZE + crypto_overhead :]
    return FragmentPacket(
        sequence=sequence,
        fragment_count=fragment_count,
        payload=body,
        original_len=original_len,
        message_id=message_id_bytes.hex(),
        payload_digest=payload_digest_bytes.hex(),
        encrypted=bool(flags & FLAG_ENCRYPTED),
        final=bool(flags & FLAG_FINAL),
        crypto_overhead=crypto_overhead,
        raw=raw,
    )


def encode_aggregate(packets: Sequence[FragmentPacket | bytes], *, mtu: int) -> bytes:
    raw_packets = [
        packet.raw if isinstance(packet, FragmentPacket) else bytes(packet)
        for packet in packets
    ]
    if not raw_packets:
        fail(E_BAD_FRAGMENT, "aggregate must contain at least one fragment")
    if len(raw_packets) > 255:
        fail(E_BAD_FRAGMENT, "aggregate contains too many fragments")
    header = AGGREGATE_STRUCT.pack(
        MAGIC_AGGREGATE,
        VERSION,
        len(raw_packets),
        0,
    )
    body = b"".join(
        ENTRY_LENGTH_STRUCT.pack(len(raw)) + raw for raw in raw_packets
    )
    envelope = header + body
    if len(envelope) > mtu:
        fail(
            E_BAD_MTU,
            "aggregate envelope exceeds MTU",
            details={"actual": len(envelope), "mtu": mtu},
        )
    return envelope


def decode_outer(raw: bytes) -> tuple[str, list[FragmentPacket]]:
    """Decode a fragment datagram or an aggregate envelope."""

    if raw.startswith(MAGIC_FRAGMENT):
        return "fragment", [parse_fragment(raw)]
    if not raw.startswith(MAGIC_AGGREGATE):
        fail(E_BAD_FRAGMENT, "unknown outer datagram magic")
    if len(raw) < AGGREGATE_HEADER_SIZE:
        fail(E_BAD_FRAGMENT, "aggregate shorter than header")
    magic, version, count, reserved = AGGREGATE_STRUCT.unpack(
        raw[:AGGREGATE_HEADER_SIZE]
    )
    if magic != MAGIC_AGGREGATE or version != VERSION or reserved != 0:
        fail(E_BAD_FRAGMENT, "malformed aggregate header")
    if count == 0:
        fail(E_BAD_FRAGMENT, "aggregate contains no fragments")
    offset = AGGREGATE_HEADER_SIZE
    packets: list[FragmentPacket] = []
    for _ in range(count):
        if offset + ENTRY_LENGTH_STRUCT.size > len(raw):
            fail(E_BAD_FRAGMENT, "truncated aggregate entry length")
        (entry_len,) = ENTRY_LENGTH_STRUCT.unpack(
            raw[offset : offset + ENTRY_LENGTH_STRUCT.size]
        )
        offset += ENTRY_LENGTH_STRUCT.size
        if offset + entry_len > len(raw):
            fail(E_BAD_FRAGMENT, "truncated aggregate fragment")
        packets.append(parse_fragment(raw[offset : offset + entry_len]))
        offset += entry_len
    if offset != len(raw):
        fail(E_BAD_FRAGMENT, "trailing bytes in aggregate")
    return "aggregate", packets


def pack_fragments(packets: Sequence[FragmentPacket], *, mtu: int) -> list[bytes]:
    """Greedily pack fragments into MTU-sized aggregate envelopes."""

    envelopes: list[bytes] = []
    current: list[FragmentPacket] = []
    current_size = AGGREGATE_HEADER_SIZE
    for packet in packets:
        entry_size = ENTRY_LENGTH_STRUCT.size + len(packet.raw)
        if entry_size + AGGREGATE_HEADER_SIZE > mtu:
            if current:
                envelopes.append(encode_aggregate(current, mtu=mtu))
                current = []
                current_size = AGGREGATE_HEADER_SIZE
            fail(
                E_BAD_MTU,
                "single fragment cannot fit aggregate envelope",
                details={"fragment_size": len(packet.raw), "mtu": mtu},
            )
        if current and current_size + entry_size > mtu:
            envelopes.append(encode_aggregate(current, mtu=mtu))
            current = []
            current_size = AGGREGATE_HEADER_SIZE
        current.append(packet)
        current_size += entry_size
    if current:
        envelopes.append(encode_aggregate(current, mtu=mtu))
    return envelopes


@dataclass(slots=True)
class ReassemblyResult:
    payload: bytes
    digest: str
    fragment_count: int
    message_id: str
    out_of_order: bool
    duplicates: int

    def as_dict(self) -> dict[str, object]:
        return {
            "digest": self.digest,
            "duplicates": self.duplicates,
            "fragment_count": self.fragment_count,
            "message_id": self.message_id,
            "out_of_order": self.out_of_order,
            "payload_len": len(self.payload),
        }


@dataclass(slots=True)
class AddResult:
    sequence: int
    duplicate: bool
    out_of_order: bool
    complete: bool


class Reassembler:
    """Stateful reassembler.

    Strict duplicate handling is enabled by default. ``tolerate_duplicates`` is
    used by the loopback receiver because network retries can legitimately
    replay an already received fragment.
    """

    def __init__(
        self,
        *,
        message_id: str | None = None,
        tolerate_duplicates: bool = False,
    ) -> None:
        self.message_id = message_id
        self.tolerate_duplicates = tolerate_duplicates
        self.fragments: dict[int, FragmentPacket] = {}
        self.final_sequence: int | None = None
        self.out_of_order = False
        self.duplicates = 0
        self._next_expected = 0
        self._complete = False
        self._result: ReassemblyResult | None = None

    @property
    def complete(self) -> bool:
        return self._complete

    def add(self, packet: FragmentPacket, *, require_contiguous: bool = False) -> AddResult:
        if self.message_id is None:
            self.message_id = packet.message_id
        elif packet.message_id != self.message_id:
            fail(
                E_BAD_FRAGMENT,
                "fragment belongs to a different message",
                details={
                    "expected": self.message_id,
                    "actual": packet.message_id,
                },
            )
        if self._complete:
            self.duplicates += 1
            return AddResult(packet.sequence, True, False, True)

        if self.fragments:
            first = self.fragments[next(iter(self.fragments))]
            if (
                packet.original_len != first.original_len
                or packet.payload_digest != first.payload_digest
            ):
                fail(
                    E_DIGEST_MISMATCH,
                    "fragment metadata conflicts with the message digest",
                    details={
                        "expected_digest": first.payload_digest,
                        "actual_digest": packet.payload_digest,
                    },
                )
        prior = self.fragments.get(packet.sequence)
        sequence_out_of_order = packet.sequence != self._next_expected
        if give_order_error := (require_contiguous and sequence_out_of_order):
            fail(
                E_OUT_OF_ORDER,
                "fragment arrived out of sequence",
                details={
                    "expected": self._next_expected,
                    "actual": packet.sequence,
                },
            )
        if prior is not None:
            if prior.raw == packet.raw:
                if not self.tolerate_duplicates:
                    fail(
                        E_DUPLICATE_FRAGMENT,
                        "duplicate fragment",
                        details={"sequence": packet.sequence},
                    )
                self.duplicates += 1
                return AddResult(packet.sequence, True, sequence_out_of_order, self._complete)
            fail(
                E_CONFLICTING_FRAGMENT,
                "conflicting fragment for the same sequence",
                details={"sequence": packet.sequence},
            )

        self.fragments[packet.sequence] = packet
        self.out_of_order = self.out_of_order or sequence_out_of_order
        while self._next_expected in self.fragments:
            self._next_expected += 1
        if packet.final:
            if self.final_sequence is not None and self.final_sequence != packet.sequence:
                fail(
                    E_BAD_FRAGMENT,
                    "multiple final fragment sequences",
                    details={
                        "first": self.final_sequence,
                        "second": packet.sequence,
                    },
                )
            self.final_sequence = packet.sequence
        self._maybe_complete()
        return AddResult(
            packet.sequence,
            False,
            sequence_out_of_order,
            self._complete,
        )

    def _missing(self) -> list[int]:
        if self.final_sequence is None:
            return []
        return [
            sequence
            for sequence in range(self.final_sequence + 1)
            if sequence not in self.fragments
        ]

    def _maybe_complete(self) -> None:
        if self._complete or self.final_sequence is None:
            return
        if self._missing():
            return
        payload = b"".join(
            self.fragments[sequence].payload
            for sequence in range(self.final_sequence + 1)
        )
        first = self.fragments[0]
        if len(payload) != first.original_len:
            fail(
                E_DIGEST_MISMATCH,
                "reassembled length does not match the original length",
                details={"actual": len(payload), "expected": first.original_len},
            )
        full_digest = hashlib.sha256(payload).digest()
        if full_digest[:16].hex() != first.payload_digest:
            fail(
                E_DIGEST_MISMATCH,
                "reassembled payload digest does not match fragment header",
            )
        self._complete = True
        self._result = ReassemblyResult(
            payload=payload,
            digest=full_digest.hex(),
            fragment_count=self.final_sequence + 1,
            message_id=first.message_id,
            out_of_order=self.out_of_order,
            duplicates=self.duplicates,
        )

    def result(self, *, finalize: bool = False) -> ReassemblyResult:
        if self._complete and self._result is not None:
            return self._result
        if finalize or self.final_sequence is not None:
            missing = self._missing()
            if missing:
                fail(
                    E_MISSING_FRAGMENT,
                    "fragment sequence is incomplete",
                    details={
                        "available": sorted(self.fragments),
                        "missing": missing,
                        "message_id": self.message_id,
                    },
                )
        fail(
            E_MISSING_FRAGMENT,
            "reassembly is not complete yet",
            details={
                "available": sorted(self.fragments),
                "final_sequence": self.final_sequence,
                "message_id": self.message_id,
            },
        )

    def summary(self) -> dict[str, object]:
        missing = self._missing()
        return {
            "complete": self._complete,
            "duplicates": self.duplicates,
            "final_sequence": self.final_sequence,
            "message_id": self.message_id,
            "missing": missing,
            "out_of_order": self.out_of_order,
            "received": len(self.fragments),
        }


def reassemble_packets(
    packets: Iterable[FragmentPacket | bytes],
    *,
    strict_order: bool = False,
    tolerate_duplicates: bool = False,
    expected_digest: str | None = None,
) -> ReassemblyResult:
    reassembler = Reassembler(tolerate_duplicates=tolerate_duplicates)
    for item in packets:
        packet = item if isinstance(item, FragmentPacket) else parse_fragment(item)
        reassembler.add(packet, require_contiguous=strict_order)
    result = reassembler.result(finalize=True)
    if expected_digest is not None and result.digest != expected_digest:
        fail(
            E_DIGEST_MISMATCH,
            "overall payload digest mismatch",
            details={"actual": result.digest, "expected": expected_digest},
        )
    return result
