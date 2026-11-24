from __future__ import annotations

import json

from smuggling_engine.constants import (
    BUSINESS_CHUNK_HEX,
    BUSINESS_CL_TE,
    BUSINESS_DUP_HEADER,
    BUSINESS_TRAILING_BYTES,
)
from smuggling_engine.engine import Engine
from smuggling_engine.generator import verify_report_against_truth
from smuggling_engine.http_analysis import analyze_request_stream

from conftest import load_sample


def _codes(data: bytes):
    analysis = analyze_request_stream(data, "unit")
    return [(finding.code, finding.byte_offset) for finding in analysis.findings]


def test_four_ambiguity_codes_and_offsets_are_stable():
    clte = (
        b"POST /x HTTP/1.1\r\nHost: a\r\n"
        b"Content-Length: 4\r\nTransfer-Encoding: chunked\r\n\r\n0\r\n\r\n"
    )
    result = _codes(clte)
    assert result == [
        (
            BUSINESS_CL_TE,
            clte.index(b"Content-Length"),
        )
    ]

    duplicate = (
        b"POST /x HTTP/1.1\r\nHost: a\r\n"
        b"Content-Length: 5\r\nContent-Length: 6\r\n\r\nhello"
    )
    result = _codes(duplicate)
    assert result == [(BUSINESS_DUP_HEADER, duplicate.rindex(b"Content-Length"))]

    chunk = (
        b"POST /x HTTP/1.1\r\nHost: a\r\nTransfer-Encoding: chunked\r\n\r\n"
        b"Z5\r\nhello\r\n0\r\n\r\n"
    )
    result = _codes(chunk)
    assert result == [(BUSINESS_CHUNK_HEX, chunk.index(b"Z5"))]

    trailing = b"POST /x HTTP/1.1\r\nHost: a\r\nContent-Length: 5\r\n\r\nhelloEXTRA"
    result = _codes(trailing)
    assert result == [(BUSINESS_TRAILING_BYTES, trailing.index(b"EXTRA"))]


def test_cl_te_prefers_rule_generation_even_with_multiple_hits(standard_sample):
    _, rejects, groups, view = load_sample(standard_sample)
    report = Engine().run(groups, view, rejected=rejects)
    connection = next(
        item for item in report.connections if item.connection_id == "conn-01-clte"
    )
    assert connection.business_code == BUSINESS_CL_TE
    assert connection.selected_rule == "cl-te.strict"
    assert connection.selected_generation == 3
    assert any(hit.canonical_name == "cl-te" for hit in connection.hits)
    assert sum(hit.selected for hit in connection.hits) == 1
    finding = next(
        item for item in connection.findings if item.code == BUSINESS_CL_TE
    )
    group = next(
        item for item in groups if item.connection_id == "conn-01-clte"
    )
    encoded = group.streams["request"].reconstructed
    assert 0 <= finding.byte_offset < len(encoded)
    assert finding.byte_end > finding.byte_start


def test_sample_verdicts_match_sync_truth(standard_sample):
    _, rejects, groups, view = load_sample(standard_sample)
    report = Engine().run(
        groups, view, rejected=rejects
    ).to_dict()
    actual = {item["connection_id"]: item for item in report["connections"]}
    for connection_id, expected in standard_sample["truth"][
        "expected_connections"
    ].items():
        connection = actual[connection_id]
        for key in ("verdict", "business_code", "selected_rule", "selected_generation", "status"):
            if key in expected:
                assert connection[key] == expected[key], (connection_id, key)
    codes = {
        finding["code"]
        for connection in report["connections"]
        for finding in connection["findings"]
    }
    assert {
        BUSINESS_CL_TE,
        BUSINESS_DUP_HEADER,
        BUSINESS_CHUNK_HEX,
        BUSINESS_TRAILING_BYTES,
    } <= codes
    verification = verify_report_against_truth(
        report, standard_sample["truth"]
    )
    assert verification == {"ok": True, "mismatches": []}


def test_report_is_serializable_json(standard_sample):
    _, rejects, groups, view = load_sample(standard_sample)
    report = Engine().run(groups, view, rejected=rejects)
    encoded = json.dumps(report.to_dict(), sort_keys=True)
    parsed = json.loads(encoded)
    assert parsed["schema_version"] == "1.0"
    assert parsed["summary"]["connection_count"] == 10

