from __future__ import annotations

import json
import random
from dataclasses import replace
from typing import List

from tlsdiag import codes
from tlsdiag.fragments import (
    KIND_CERTIFICATE,
    KIND_HANDSHAKE,
    make_envelope,
    reassemble,
)
from tlsdiag.models import RawRecord


def _record(connection_id: str, seq: int, payload: bytes, line_no: int) -> RawRecord:
    return RawRecord(
        connection_id=connection_id,
        direction="server_to_client",
        record_seq=seq,
        timestamp_ms=1000 + seq,
        record_type="fragment",
        payload_hex=payload.hex(),
        payload=payload,
        source_file="unit.jsonl",
        line_no=line_no,
        arrival_index=line_no,
    )


def _envelopes(
    connection_id: str,
    message_id: int,
    kind: int,
    payload: bytes,
    chunk_size: int,
) -> List[RawRecord]:
    records: List[RawRecord] = []
    seq = 0
    for offset in range(0, len(payload), chunk_size):
        envelope = make_envelope(
            connection_id=connection_id,
            message_id=message_id,
            kind=kind,
            declared_length=len(payload),
            offset=offset,
            data=payload[offset : offset + chunk_size],
        )
        records.append(_record(connection_id, seq, envelope, seq + 1))
        seq += 1
    return records


def test_reassembly_is_arrival_order_independent() -> None:
    payload = json.dumps(
        {"kind": "certificate", "certificates": [{"subject": "CN=L"}]},
        separators=(",", ":"),
    ).encode()
    records = _envelopes("conn-a", 7, KIND_CERTIFICATE, payload, 5)
    baseline = reassemble(records)
    assert baseline.status == "ok"
    assert baseline.messages[0].data == payload
    for seed in (1, 3, 9):
        shuffled = list(records)
        random.Random(seed).shuffle(shuffled)
        shuffled = [
            replace(record, line_no=index + 1, arrival_index=index + 1)
            for index, record in enumerate(shuffled)
        ]
        result = reassemble(shuffled)
        assert result.status == "ok"
        assert result.messages[0].data == payload
        assert result.to_dict()["messages"] == baseline.to_dict()["messages"]


def test_duplicate_first_occurrence_wins_and_is_counted() -> None:
    payload = b'{"kind":"handshake","value":1}'
    records = _envelopes("conn-a", 1, KIND_HANDSHAKE, payload, 8)
    duplicate = replace(
        records[1],
        payload=make_envelope(
            connection_id="conn-a",
            message_id=1,
            kind=KIND_HANDSHAKE,
            declared_length=len(payload),
            offset=8,
            data=b"XXXXXXXX",
        ),
        line_no=99,
        arrival_index=99,
    )
    result = reassemble(records + [duplicate])
    assert result.duplicate_count == 1
    assert result.duplicate_conflicts == 1
    assert result.messages[0].data == payload
    assert result.status == "ok"


def test_sequence_gap_has_explicit_missing_range() -> None:
    payload = b'{"kind":"handshake"}'
    records = _envelopes("conn-a", 1, KIND_HANDSHAKE, payload, 2)
    result = reassemble([r for r in records if r.record_seq != 2])
    assert result.status == "gap"
    assert result.missing_sequence_ranges == [(2, 2)]
    assert codes.E_FRAG_GAP in {i.code for i in result.issues}


def test_cross_connection_stream_tag_is_mixup() -> None:
    payload = b'{"kind":"handshake"}'
    records = _envelopes("conn-a", 1, KIND_HANDSHAKE, payload, 4)
    foreign = make_envelope(
        connection_id="conn-b",
        message_id=1,
        kind=KIND_HANDSHAKE,
        declared_length=len(payload),
        offset=0,
        data=payload,
    )
    records.append(_record("conn-a", len(records), foreign, 100))
    result = reassemble(records)
    assert result.status == "mixup"
    assert result.mixup_records == [len(records) - 1]
    assert codes.E_MIXUP in {i.code for i in result.issues}
    assert result.messages[0].data == payload


def test_declared_length_and_overlap_are_checked() -> None:
    payload = b'{"kind":"handshake","padding":"1234567890"}'
    truncated = make_envelope(
        connection_id="conn-a",
        message_id=1,
        kind=KIND_HANDSHAKE,
        declared_length=len(payload) + 5,
        offset=0,
        data=payload,
    )
    result = reassemble([_record("conn-a", 0, truncated, 1)])
    assert not result.messages[0].length_ok
    assert codes.E_FRAG_GAP in {i.code for i in result.issues}

    first = make_envelope(
        connection_id="conn-a",
        message_id=2,
        kind=KIND_HANDSHAKE,
        declared_length=10,
        offset=0,
        data=b"12345",
    )
    second = make_envelope(
        connection_id="conn-a",
        message_id=2,
        kind=KIND_HANDSHAKE,
        declared_length=10,
        offset=0,
        data=b"abcde",
    )
    overlap = reassemble(
        [
            _record("conn-a", 0, first, 1),
            _record("conn-a", 1, second, 2),
        ]
    )
    assert codes.E_FRAG_OVERLAP in {i.code for i in overlap.issues}


def test_raw_length_prefixed_fallback() -> None:
    import struct

    first = json.dumps({"kind": "certificate", "certificates": []}).encode()
    second = json.dumps({"kind": "handshake", "observed_time_ms": 1}).encode()
    blob = struct.pack(">I", len(first)) + first + struct.pack(">I", len(second)) + second
    records = [
        _record("conn-raw", 0, blob[: len(blob) // 2], 1),
        _record("conn-raw", 1, blob[len(blob) // 2 :], 2),
    ]
    result = reassemble(records)
    assert result.status == "ok"
    assert [m.kind for m in result.messages] == ["certificate", "handshake"]
