from __future__ import annotations

import pytest

from tunnel_kernel.codec import (
    HEADER_SIZE,
    Reassembler,
    decode_outer,
    encode_aggregate,
    fragment_payload,
    parse_fragment,
    reassemble_packets,
    sha256_hex,
)
from tunnel_kernel.config import synthetic_payload
from tunnel_kernel.errors import (
    E_BAD_FRAGMENT,
    E_BAD_MTU,
    E_CONFLICTING_FRAGMENT,
    E_DIGEST_MISMATCH,
    E_DUPLICATE_FRAGMENT,
    E_MISSING_FRAGMENT,
    E_OUT_OF_ORDER,
    BusinessError,
)


MESSAGE_ID = "0123456789abcdef0123456789abcdef"


def _frags(payload: bytes, *, encrypted: bool = True):
    return fragment_payload(
        payload,
        message_id=MESSAGE_ID,
        mtu=1200,
        fragment_threshold_bytes=512,
        encrypted=encrypted,
        crypto_overhead_bytes=24,
    )


def test_fragmentation_is_byte_deterministic() -> None:
    payload = synthetic_payload("deterministic", 1024, 9)
    first = _frags(payload)
    second = _frags(payload)
    assert [packet.raw for packet in first] == [packet.raw for packet in second]
    assert [packet.digest for packet in first] == [packet.digest for packet in second]
    assert [packet.sequence for packet in first] == [0, 1, 2]
    assert first[-1].final
    assert not any(packet.final for packet in first[:-1])
    assert all(len(packet.raw) <= 512 for packet in first)
    assert first[0].crypto_overhead == 24
    assert first[0].raw[HEADER_SIZE : HEADER_SIZE + 24] == b"\x00" * 24


def test_encryption_overhead_changes_capacity() -> None:
    payload = synthetic_payload("overhead", 880, 1)
    encrypted = _frags(payload, encrypted=True)
    plain = _frags(payload, encrypted=False)
    assert len(encrypted) > len(plain)
    assert all(packet.crypto_overhead == 24 for packet in encrypted)
    assert all(packet.crypto_overhead == 0 for packet in plain)


def test_out_of_order_reassembly_matches_digest() -> None:
    payload = synthetic_payload("reorder", 1000, 11)
    packets = _frags(payload)
    result = reassemble_packets([packets[2], packets[0], packets[1]])
    assert result.payload == payload
    assert result.digest == sha256_hex(payload)
    assert result.out_of_order is True
    assert result.fragment_count == 3


def test_strict_order_maps_to_out_of_order_error() -> None:
    payload = synthetic_payload("strict", 1000, 2)
    packets = _frags(payload)
    with pytest.raises(BusinessError) as caught:
        reassemble_packets([packets[1], packets[0], packets[2]], strict_order=True)
    assert caught.value.code == E_OUT_OF_ORDER


def test_missing_fragment_maps_to_missing_error() -> None:
    payload = synthetic_payload("missing", 1000, 3)
    packets = _frags(payload)
    with pytest.raises(BusinessError) as caught:
        reassemble_packets([packets[0], packets[2]])
    assert caught.value.code == E_MISSING_FRAGMENT
    assert caught.value.details["missing"] == [1]


def test_duplicate_fragment_maps_to_duplicate_error() -> None:
    payload = synthetic_payload("duplicate", 1000, 4)
    packets = _frags(payload)
    with pytest.raises(BusinessError) as caught:
        reassemble_packets([packets[0], packets[0], packets[1], packets[2]])
    assert caught.value.code == E_DUPLICATE_FRAGMENT


def test_tolerant_reassembler_counts_duplicate() -> None:
    payload = synthetic_payload("tolerant", 1000, 5)
    packets = _frags(payload)
    reassembler = Reassembler(tolerate_duplicates=True)
    for packet in [
        packets[0],
        packets[0],
        packets[1],
        packets[2],
    ]:
        reassembler.add(packet)
    result = reassembler.result()
    assert result.payload == payload
    assert result.duplicates == 1


def test_conflicting_duplicate_maps_to_conflicting_error() -> None:
    payload = synthetic_payload("conflict", 1000, 6)
    packets = _frags(payload)
    tampered = bytearray(packets[0].raw)
    tampered[-1] ^= 0xFF
    reassembler = Reassembler(tolerate_duplicates=True)
    reassembler.add(packets[0])
    with pytest.raises(BusinessError) as caught:
        reassembler.add(parse_fragment(bytes(tampered)))
    assert caught.value.code == E_CONFLICTING_FRAGMENT


def test_body_corruption_maps_to_digest_mismatch() -> None:
    payload = synthetic_payload("corrupt", 1000, 7)
    packets = _frags(payload)
    tampered = bytearray(packets[1].raw)
    tampered[-1] ^= 0xFF
    with pytest.raises(BusinessError) as caught:
        reassemble_packets([packets[0], bytes(tampered), packets[2]])
    assert caught.value.code == E_DIGEST_MISMATCH


def test_header_corruption_maps_to_bad_fragment() -> None:
    payload = synthetic_payload("header", 1000, 8)
    packet = _frags(payload)[0]
    tampered = bytearray(packet.raw)
    tampered[10] ^= 0x01
    with pytest.raises(BusinessError) as caught:
        parse_fragment(bytes(tampered))
    assert caught.value.code in {E_BAD_FRAGMENT, E_DIGEST_MISMATCH}


def test_aggregate_roundtrip_and_mtu_limit() -> None:
    payload = synthetic_payload("aggregate", 1000, 10)
    packets = _frags(payload)
    envelope = encode_aggregate(packets[:2], mtu=1200)
    kind, decoded = decode_outer(envelope)
    assert kind == "aggregate"
    assert [packet.sequence for packet in decoded] == [0, 1]
    result = reassemble_packets([*decoded, packets[2]])
    assert result.payload == payload
    with pytest.raises(BusinessError) as caught:
        encode_aggregate(packets[:2], mtu=100)
    assert caught.value.code == E_BAD_MTU


def test_replanned_tail_uses_original_digest_and_length() -> None:
    payload = synthetic_payload("replan-codec", 1024, 12)
    first = _frags(payload)
    remaining = payload[848:]
    tail = fragment_payload(
        remaining,
        message_id=MESSAGE_ID,
        mtu=500,
        fragment_threshold_bytes=512,
        encrypted=True,
        crypto_overhead_bytes=24,
        start_sequence=2,
        original_len=len(payload),
        original_digest=sha256_hex(payload),
    )
    result = reassemble_packets([first[0], first[1], *tail])
    assert result.payload == payload
    assert result.digest == sha256_hex(payload)
