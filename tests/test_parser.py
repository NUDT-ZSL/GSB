from __future__ import annotations

from pathlib import Path

from authdiag.parser import (
    parse_event_file,
    parse_policy_document,
    validate_entry_drafts,
    validate_raw_event,
)


def _codes(diagnostics) -> set[str]:
    return {item.code for item in diagnostics}


def test_invalid_policy_items_are_rejected_individually(sample_dir: Path, read_json):
    document = read_json(sample_dir / "invalid_policy.json")
    result = parse_policy_document(document, source="invalid_policy.json")
    entries, validation = validate_entry_drafts(
        result.base_drafts,
        default_source="invalid_policy.json",
    )

    codes = _codes(result.diagnostics) | _codes(validation)
    assert {
        "E_VERSION",
        "E_ROLE_REF",
        "E_TOKEN_REF",
        "E_DUP",
        "E_SCHEMA",
    }.issubset(codes)
    assert "role:base" in entries
    assert "session:base" in entries
    assert "token:good" in entries
    assert "binding:good" in entries
    assert "role:badver" not in entries
    assert "role:orphan" not in entries
    assert "token:broken" not in entries
    assert "session:temp" not in entries

    version_errors = [
        item
        for item in result.diagnostics
        if item.code == "E_VERSION"
    ]
    assert version_errors
    assert version_errors[0].index == 1
    assert version_errors[0].ordinal == 2


def test_event_validation_continues_after_bad_items(sample_dir: Path):
    raw = (sample_dir / "invalid_events.json").read_text(encoding="utf-8")
    result = parse_event_file("invalid_events.json", raw)
    event_validation = [
        diagnostic
        for event in result.events
        for diagnostic in validate_raw_event(event)
    ]
    codes = _codes(result.diagnostics) | _codes(event_validation)
    assert {
        "E_SESSION_FORMAT",
        "E_TOKEN_FORMAT",
        "E_EVENT_TYPE",
        "E_EVENT_DUP",
    }.issubset(codes)
    assert any(event.event_id == "evt-good" for event in result.events)
    format_errors = [
        item for item in event_validation if item.code == "E_SESSION_FORMAT"
    ]
    assert format_errors[0].offset is not None
    assert format_errors[0].index == 1


def test_malformed_event_file_is_file_scoped(sample_dir: Path):
    raw = (sample_dir / "events_malformed.json").read_text(encoding="utf-8")
    result = parse_event_file("events_malformed.json", raw)
    assert result.status == "failed"
    assert _codes(result.diagnostics) == {"E_JSON"}
    assert result.diagnostics[0].offset is not None


def test_missing_references_cascade_to_dependents():
    document = {
        "schema_version": "1.0",
        "policy_version": "1.0.0",
        "entries": [
            {
                "id": "role:child",
                "kind": "role",
                "version": "1.0.0",
                "parents": ["role:gone"],
            },
            {
                "id": "binding:x",
                "kind": "role_binding",
                "version": "1.0.0",
                "subject": "x",
                "role": "role:child",
                "token_rule": "token:x",
                "session_policy": "session:x",
            },
            {
                "id": "token:x",
                "kind": "token_rule",
                "version": "1.0.0",
                "refs": ["role:child"],
            },
            {"id": "session:x", "kind": "session_policy", "version": "1.0.0"},
        ],
    }
    result = parse_policy_document(document)
    entries, diagnostics = validate_entry_drafts(
        result.base_drafts,
        default_source="p",
    )
    assert "role:child" not in entries
    assert "token:x" not in entries
    assert "binding:x" not in entries
    assert _codes(diagnostics) == {"E_ROLE_REF", "E_TOKEN_REF", "E_BINDING_REF"}


def test_event_jsonl_offsets():
    raw = (
        '{"event_id":"evt-a","type":"logout","session_id":"sess-a"}\n'
        '{"event_id":"evt-b","type":"logout","session_id":"sess-b"}\n'
    )
    result = parse_event_file("events.jsonl", raw)
    assert len(result.events) == 2
    assert result.events[0].offset == 0
    assert result.events[1].offset == raw.index("{") + raw.index("\n") + 1


def test_event_jsonl_bad_line_does_not_discard_following_events():
    raw = (
        '{"event_id":"evt-a","type":"logout","session_id":"sess-a"}\n'
        "{bad json}\n"
        '{"event_id":"evt-b","type":"logout","session_id":"sess-b"}\n'
    )
    result = parse_event_file("events.jsonl", raw)
    assert result.status == "partial"
    assert [event.event_id for event in result.events] == ["evt-a", "evt-b"]
    error = next(item for item in result.diagnostics if item.code == "E_JSON")
    assert error.index == 1
    assert error.offset is not None


def test_index_and_offset_are_in_rejection_record(sample_dir: Path, read_json):
    document = read_json(sample_dir / "invalid_policy.json")
    result = parse_policy_document(document, source="invalid_policy.json")
    rejection = next(item for item in result.diagnostics if item.code == "E_VERSION")
    payload = rejection.to_dict()
    assert payload["index"] == 1
    assert payload["ordinal"] == 2
    assert "code" in payload
