from __future__ import annotations

import io

from audit_forge.engine import AuditEngine
from audit_forge.rules import default_rule_config
from audit_forge.validation import normalize_line


def test_normalize_line_rejects_with_line_numbers_and_continues() -> None:
    raw = "\n".join(
        [
            '{"event_id":"ok","trace_id":"t","actor":"a","action":"x",'
            '"object":"o","ts":1,"seq":1}',
            "",
            "{bad json",
            '{"event_id":"missing","trace_id":"t"}',
            '{"event_id":"typed","trace_id":"t","actor":1,"action":"x",'
            '"object":"o","ts":2,"seq":2}',
            '{"event_id":"bool","trace_id":"t","actor":"a","action":"x",'
            '"object":"o","ts":true,"seq":2}',
            '{"event_id":"neg","trace_id":"t","actor":"a","action":"x",'
            '"object":"o","ts":-1,"seq":3}',
            '{"event_id":"zero","trace_id":"t","actor":"a","action":"x",'
            '"object":"o","ts":3,"seq":0}',
        ]
    ).encode("utf-8") + b"\n"

    engine = AuditEngine(default_rule_config())
    engine.ingest_stream(io.BytesIO(raw), "validation.jsonl")

    assert list(engine.events) == ["ok"]
    rejects = sorted(engine.rejects.values(), key=lambda item: item["line"])
    assert [reject["code"] for reject in rejects] == [
        "E_EMPTY_LINE",
        "E_JSON",
        "E_MISSING_FIELD",
        "E_INVALID_TYPE",
        "E_INVALID_TYPE",
        "E_TS_RANGE",
        "E_SEQ_RANGE",
    ]
    assert [reject["line"] for reject in rejects] == [2, 3, 4, 5, 6, 7, 8]
    assert engine.stats["lines_read"] == 8
    assert engine.stats["rejected_lines"] == 7


def test_normalize_line_rejects_invalid_utf8_and_non_object() -> None:
    event, reject = normalize_line(b"\xff\xfe\n", 1, "bad.jsonl")
    assert event is None
    assert reject is not None
    assert reject["code"] == "E_ENCODING"

    event, reject = normalize_line(b"[]\n", 2, "bad.jsonl")
    assert event is None
    assert reject is not None
    assert reject["code"] == "E_NOT_OBJECT"


def test_optional_parent_null_is_valid() -> None:
    line = (
        b'{"event_id":"e","trace_id":"t","actor":"a","action":"x",'
        b'"object":"o","ts":0,"seq":1,"parent_event_id":null}\n'
    )
    event, reject = normalize_line(line, 1, "ok.jsonl")
    assert reject is None
    assert event is not None
    assert event.parent_event_id is None

