from __future__ import annotations

import json
import random
from pathlib import Path

from smuggling_engine.engine import group_fingerprint
from smuggling_engine.parsing import group_connections, parse_messages

from conftest import load_sample


def test_invalid_records_are_rejected_and_stream_continues():
    payload = b"POST /ok HTTP/1.1\r\nHost: example\r\n\r\n"
    lines = [
        "{bad json}\n",
        json.dumps(
            {
                "connection_id": "bad id",
                "direction": "request",
                "sequence": 1,
                "timestamp_ms": 0,
                "payload_hex": payload.hex(),
            }
        ),
        json.dumps(
            {
                "connection_id": "ok",
                "direction": "sideways",
                "sequence": 1,
                "timestamp_ms": 0,
                "payload_hex": payload.hex(),
            }
        ),
        json.dumps(
            {
                "connection_id": "ok",
                "direction": "request",
                "sequence": 1,
                "timestamp_ms": -1,
                "payload_hex": payload.hex(),
            }
        ),
        json.dumps(
            {
                "connection_id": "ok",
                "direction": "request",
                "sequence": 1,
                "timestamp_ms": 0,
                "payload_hex": "abc",
            }
        ),
        json.dumps(
            {
                "connection_id": "ok",
                "direction": "request",
                "sequence": 1,
                "timestamp_ms": 0,
                "payload_hex": payload.hex(),
                "sync_marker": "SYNC-OK",
            }
        ),
        json.dumps(
            {
                "connection_id": "ok",
                "direction": "request",
                "sequence": 1,
                "timestamp_ms": 0,
                "payload_hex": payload.hex(),
                "sync_marker": "SYNC-DUP",
            }
        ),
    ]
    messages, rejects = parse_messages(lines)
    assert len(messages) == 1
    assert messages[0].sync_marker == "SYNC-OK"
    assert messages[0].duplicate_count == 1
    assert messages[0].duplicate_conflicts == 0
    assert messages[0].duplicate_bytes == len(payload)
    assert [item.record_index for item in rejects] == [1, 2, 3, 4, 5]
    assert all(item.source == "raw_message" for item in rejects)
    assert all(item.to_dict()["record_index"] == item.record_index for item in rejects)


def test_duplicate_first_payload_wins_and_conflict_is_counted():
    first = b"GET / HTTP/1.1\r\nHost: a\r\n\r\n"
    second = b"GET /changed HTTP/1.1\r\nHost: a\r\n\r\n"
    lines = [
        json.dumps(
            {
                "connection_id": "c1",
                "direction": "request",
                "sequence": 1,
                "timestamp_ms": 1,
                "payload_hex": first.hex(),
            }
        ),
        json.dumps(
            {
                "connection_id": "c1",
                "direction": "request",
                "sequence": 1,
                "timestamp_ms": 99,
                "payload_hex": second.hex(),
            }
        ),
    ]
    messages, rejects = parse_messages(lines)
    assert not rejects
    assert messages[0].payload == first
    assert messages[0].duplicate_count == 1
    assert messages[0].duplicate_conflicts == 1


def test_arrival_order_does_not_change_reassembly(standard_sample):
    directory = Path(standard_sample["directory"])
    lines = (directory / "messages.jsonl").read_text(encoding="utf-8").splitlines()
    original_messages, original_rejects = parse_messages(lines)
    original_groups, _ = group_connections(original_messages)
    original = {
        group.connection_id: (
            group_fingerprint(group),
            {
                direction: (
                    stream.reconstructed.hex(),
                    stream.missing_ranges,
                    stream.duplicate_count,
                )
                for direction, stream in group.streams.items()
            },
        )
        for group in original_groups
    }
    for seed in range(5):
        shuffled = list(lines)
        random.Random(seed).shuffle(shuffled)
        messages, rejects = parse_messages(shuffled)
        groups, _ = group_connections(messages)
        assert not rejects
        actual = {
            group.connection_id: (
                group_fingerprint(group),
                {
                    direction: (
                        stream.reconstructed.hex(),
                        stream.missing_ranges,
                        stream.duplicate_count,
                    )
                    for direction, stream in group.streams.items()
                },
            )
            for group in groups
        }
        assert actual == original


def test_gap_and_mixup_markers_match_truth(standard_sample):
    _, _, groups, _ = load_sample(standard_sample)
    by_id = {group.connection_id: group for group in groups}
    assert by_id["conn-08-gap"].gaps() == [
        {"direction": "request", "missing_ranges": [[2, 2]]}
    ]
    assert by_id["conn-06-mixup-a"].mixup_sequences == [1]
    assert by_id["conn-07-mixup-b"].mixup_sequences == [1]
    assert by_id["conn-01-clte"].delivered_message_count == 3
    assert by_id["conn-01-clte"].duplicate_count == 1


def test_fragment_split_and_concatenation_are_equivalent(standard_sample):
    _, _, groups, _ = load_sample(standard_sample)
    group = next(item for item in groups if item.connection_id == "conn-01-clte")
    stream = group.streams["request"]
    assert stream.reconstructed.startswith(b"POST /checkout HTTP/1.1")
    concatenated = b"".join(
        message.payload for message in sorted(stream.messages, key=lambda item: item.sequence)
    )
    assert concatenated == stream.reconstructed

