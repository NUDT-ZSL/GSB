from __future__ import annotations

from smuggle_detector.httpstream import Deadline, reassemble_direction
from smuggle_detector.models import MessageRecord


def record(
    seq: int,
    payload: bytes,
    *,
    ts: int = 0,
    connection_id: str = "conn",
    direction: str = "c2s",
) -> MessageRecord:
    return MessageRecord(
        connection_id=connection_id,
        direction=direction,
        seq=seq,
        ts_ms=ts,
        payload=payload,
        source_category="messages",
        record_index=seq + 1,
    )


def request(path: str, sync: str, body: bytes = b"", extra: bytes = b"") -> bytes:
    return (
        f"POST {path} HTTP/1.1\r\n"
        f"Host: example.test\r\nX-Det-Sync-Id: {sync}\r\n"
        f"X-Det-Sync-Conn: conn\r\n"
    ).encode() + extra + b"\r\n" + body


def test_content_length_boundaries_split_pipelined_messages() -> None:
    first = request(
        "/one",
        "one",
        b"abc",
        b"Content-Length: 3\r\n",
    )
    second = request("/two", "two", b"", b"Content-Length: 0\r\n")
    stream = reassemble_direction(
        [record(0, first + second)],
        connection_id="conn",
        direction="c2s",
        deadline=Deadline(1000),
    )
    assert not stream.errors
    assert [item.sync_id for item in stream.messages] == ["one", "two"]
    assert stream.messages[0].end_offset == len(first)
    assert stream.messages[1].start_offset == len(first)


def test_chunked_boundaries_are_not_connection_end_only() -> None:
    chunked = (
        request(
            "/chunk",
            "chunk",
            b"3\r\nabc\r\n0\r\n\r\n",
            b"Transfer-Encoding: chunked\r\n",
        )
        + request("/after", "after", b"", b"Content-Length: 0\r\n")
    )
    stream = reassemble_direction(
        [record(0, chunked)],
        connection_id="conn",
        direction="c2s",
        deadline=Deadline(1000),
    )
    assert [item.sync_id for item in stream.messages] == ["chunk", "after"]
    assert stream.messages[0].framing == "chunked"
    assert not stream.errors


def test_out_of_order_and_duplicate_first_logical_delivery_wins() -> None:
    original = request("/original", "original")
    conflicting = request("/conflicting", "conflicting")
    stream = reassemble_direction(
        [
            record(1, request("/second", "second"), ts=10),
            record(0, conflicting, ts=20),
            record(0, original, ts=5),
        ],
        connection_id="conn",
        direction="c2s",
        deadline=Deadline(1000),
    )
    assert [item.sync_id for item in stream.messages] == ["original", "second"]
    assert stream.duplicate_count == 1
    assert stream.duplicates[0]["conflicting_payloads"] is True
    assert stream.messages[0].start_offset == 0


def test_gap_reports_missing_interval_and_keeps_other_segments() -> None:
    stream = reassemble_direction(
        [
            record(0, request("/a", "a")),
            record(3, request("/d", "d"), ts=30),
        ],
        connection_id="conn",
        direction="c2s",
        deadline=Deadline(1000),
    )
    assert stream.missing_ranges == [[1, 2]]
    assert [item["code"] for item in stream.errors] == ["E_MSG_GAP"]
    assert [item.sync_id for item in stream.messages] == ["a", "d"]


def test_mixed_connection_marker_returns_e_mixed_conn() -> None:
    payload = (
        b"GET / HTTP/1.1\r\nHost: example.test\r\n"
        b"X-Det-Sync-Conn: other-conn\r\n\r\n"
    )
    stream = reassemble_direction(
        [record(0, payload)],
        connection_id="conn",
        direction="c2s",
        deadline=Deadline(1000),
    )
    assert [item["code"] for item in stream.errors] == ["E_MIXED_CONN"]


def test_chunk_size_trailer_and_boundary_residue_candidates() -> None:
    malformed = reassemble_direction(
        [
            record(
                0,
                request(
                    "/bad",
                    "bad",
                    b"Z\r\n",
                    b"Transfer-Encoding: chunked\r\n",
                ),
            )
        ],
        connection_id="conn",
        direction="c2s",
        deadline=Deadline(1000),
    )
    assert [item.code for item in malformed.candidates] == ["E_CHUNK_SIZE"]

    trailer = reassemble_direction(
        [
            record(
                0,
                request(
                    "/trailer",
                    "trailer",
                    b"1\r\na\r\n0\r\nBROKEN\r\n\r\n",
                    b"Transfer-Encoding: chunked\r\n",
                ),
            )
        ],
        connection_id="conn",
        direction="c2s",
        deadline=Deadline(1000),
    )
    assert [item.code for item in trailer.candidates] == ["E_CHUNK_TRAILER"]

    residue = reassemble_direction(
        [record(0, b"JUNK\r\n" + request("/ok", "ok"))],
        connection_id="conn",
        direction="c2s",
        deadline=Deadline(1000),
    )
    assert [item.code for item in residue.candidates] == ["E_BOUNDARY_RESIDUE"]
    assert residue.candidates[0].offset == 0
    assert residue.messages[0].start_offset == 6

