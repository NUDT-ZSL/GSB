from __future__ import annotations

import json

from tlsdiag import codes
from tlsdiag.normalize import group_records, normalize_direction, parse_handshake_jsonl


def test_invalid_records_are_reported_and_skipped() -> None:
    lines = [
        {"connection_id": "c1", "direction": "server_to_client", "record_seq": 0, "timestamp_ms": 1, "record_type": "fragment", "payload_hex": "00"},
        "{bad json",
        {"connection_id": "bad id!", "direction": "server_to_client", "record_seq": 0, "timestamp_ms": 1, "record_type": "fragment", "payload_hex": "00"},
        {"connection_id": "c1", "direction": "sideways", "record_seq": 1, "timestamp_ms": 1, "record_type": "fragment", "payload_hex": "00"},
        {"connection_id": "c1", "direction": "server_to_client", "record_seq": -1, "timestamp_ms": 1, "record_type": "fragment", "payload_hex": "00"},
        {"connection_id": "c1", "direction": "server_to_client", "record_seq": 2, "timestamp_ms": 1, "record_type": "fragment", "payload_hex": "0"},
        {"connection_id": "c1", "direction": "server_to_client", "record_seq": 3, "timestamp_ms": "not-int", "record_type": "fragment", "payload_hex": "00"},
        {"connection_id": "c1", "direction": "s2c", "record_seq": 4, "timestamp_ms": 1, "record_type": "fragment", "payload_hex": "a1"},
    ]
    data = ("\n".join(line if isinstance(line, str) else json.dumps(line) for line in lines)).encode()
    records, diagnostics = parse_handshake_jsonl(data, "stream.jsonl")
    assert [r.record_seq for r in records] == [0, 4]
    assert records[1].direction == "server_to_client"
    codes_found = {d.code for d in diagnostics}
    assert {
        codes.E_JSON,
        codes.E_CONN_ID,
        codes.E_DIRECTION,
        codes.E_SEQ,
        codes.E_HEX,
        codes.E_TIMESTAMP,
    } <= codes_found
    for diagnostic in diagnostics:
        assert diagnostic.source_file == "stream.jsonl"
        assert diagnostic.record_index is not None


def test_direction_aliases_and_grouping() -> None:
    assert normalize_direction("c2s") == "client_to_server"
    assert normalize_direction("SERVER->CLIENT") == "server_to_client"
    assert normalize_direction("sideways") is None
    lines = [
        {"connection_id": "b", "direction": "s2c", "record_seq": 1, "timestamp_ms": 1, "record_type": "fragment", "payload_hex": "00"},
        {"connection_id": "a", "direction": "c2s", "record_seq": 2, "timestamp_ms": 1, "record_type": "fragment", "payload_hex": "00"},
        {"connection_id": "a", "direction": "c2s", "record_seq": 0, "timestamp_ms": 1, "record_type": "fragment", "payload_hex": "00"},
    ]
    records, diagnostics = parse_handshake_jsonl(
        ("\n".join(json.dumps(x) for x in lines)).encode(), "stream.jsonl"
    )
    assert diagnostics == []
    groups = group_records(records)
    assert list(groups) == ["b", "a"]
    assert [r.record_seq for r in groups["a"]] == [2, 0]


def test_empty_stream() -> None:
    records, diagnostics = parse_handshake_jsonl(b"", "empty.jsonl")
    assert records == []
    assert diagnostics[0].code == codes.E_EMPTY_INPUT
