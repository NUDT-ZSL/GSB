from __future__ import annotations

import json

from audit_masker.masking import mask_value
from audit_masker.parsing import parse_events_file, parse_rules_snapshot

from conftest import event, snapshot, v2_rule, write_json, write_jsonl


def test_event_rejections_do_not_stop_batch(tmp_path):
    path = tmp_path / "events.jsonl"
    good_one = event("e-1")
    good_two = event("e-2", seq=1, category="auth", payload={"token": "x"})
    lines = [
        json.dumps(good_one),
        "{broken",
        json.dumps({**event("e-bad-payload"), "payload": {"x": 3}}),
        json.dumps({**event("e-bad-key"), "idempotency_key": ""}),
        json.dumps(event("e-1")),
        json.dumps({**event("e-neg"), "ts_ms": -1}),
        json.dumps(good_two),
    ]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    events, rejections = parse_events_file(path)
    assert [item.event_id for item in events] == ["e-1", "e-2"]
    assert len(rejections) == 5
    assert all(item.source_format == "events-jsonl" for item in rejections)
    assert {item.record_index for item in rejections} == {2, 3, 4, 5, 6}
    assert "E_DUPLICATE_EVENT" in {item.code for item in rejections}


def test_duplicate_seq_within_event_is_rejected(tmp_path):
    path = write_jsonl(
        tmp_path / "events.jsonl",
        [event("same", seq=4), event("same", seq=4, payload={"email": "b@x"})],
    )
    events, rejections = parse_events_file(path)
    assert len(events) == 1
    assert rejections[0].code == "E_DUPLICATE_EVENT"


def test_duplicate_event_id_with_new_seq_is_rejected(tmp_path):
    path = write_jsonl(
        tmp_path / "events.jsonl",
        [event("same", seq=0), event("same", seq=1)],
    )
    events, rejections = parse_events_file(path)
    assert len(events) == 1
    assert rejections[0].code == "E_DUPLICATE_EVENT_ID"


def test_v1_clock_conversion_and_strict_field_count(tmp_path):
    doc = snapshot(
        clock_ms=1_700_000_000_000,
        relative_seconds=[0, 7],
        v1=[
            "clock-rule|payload.email|email|10",
            "clock-rule-2|payload.phone|last4|20",
            "bad|payload.x|redact|1|extra",
        ],
        v2=[
            v2_rule(
                "absolute",
                effective_ms=1_700_000_123_456,
                generation=2,
            )
        ],
    )
    parsed = parse_rules_snapshot(write_json(tmp_path / "rules.json", doc))
    assert parsed.valid
    assert len(parsed.records) == 3
    by_name = {record.rule_name: record for record in parsed.records}
    assert by_name["clock-rule"].effective_ms == 1_700_000_000_000
    assert by_name["clock-rule-2"].effective_ms == 1_700_000_007_000
    assert by_name["absolute"].effective_ms == 1_700_000_123_456
    assert parsed.rejections[0].code == "E_V1_FIELDS"
    assert parsed.rejections[0].record_index == 3


def test_v2_required_fields_and_invalid_record_continuation(tmp_path):
    doc = snapshot(
        v2=[
            v2_rule("ok"),
            {
                "rule_name": "missing-tombstone",
                "field_path": "payload.email",
                "mask": "redact",
                "threshold": 1,
                "effective_ms": 1_760_000_000_000,
                "generation": 1,
            },
            {
                **v2_rule("bad-generation"),
                "generation": "two",
            },
        ]
    )
    parsed = parse_rules_snapshot(write_json(tmp_path / "rules.json", doc))
    assert [record.rule_name for record in parsed.records] == ["ok"]
    assert [item.code for item in parsed.rejections] == [
        "E_TOMBSTONE",
        "E_GENERATION",
    ]


def test_mask_methods_are_whitelisted(tmp_path):
    doc = snapshot(
        v1=["bad-mask|payload.email|rot13|1"],
        masks=["keep", "redact"],
    )
    parsed = parse_rules_snapshot(write_json(tmp_path / "rules.json", doc))
    assert not parsed.records
    assert parsed.rejections[0].code == "E_MASK_NOT_ALLOWED"


def test_v2_key_value_lines_are_supported(tmp_path):
    doc = {
        "observation_clock_ms": 1_760_000_000_000,
        "v2": [
            "rule_name=kv-rule;field_path=payload.email;mask=redact;"
            "threshold=9;effective_ms=1760000004000;generation=2;tombstone=false"
        ],
        "layout": {
            "mask_methods": ["keep", "redact", "email", "last4", "hash"],
            "threshold_mode": "higher_wins",
            "version": "layout-v1",
        },
    }
    parsed = parse_rules_snapshot(write_json(tmp_path / "rules.json", doc))
    assert len(parsed.records) == 1
    record = parsed.records[0]
    assert record.source_format == "v2-key-value"
    assert record.rule_name == "kv-rule"
    assert record.threshold == 9
    assert record.generation == 2


def test_malformed_snapshot_is_fatal_instead_of_silently_empty(tmp_path):
    path = tmp_path / "bad_rules.txt"
    path.write_text("this is not a rule snapshot\n", encoding="utf-8")
    parsed = parse_rules_snapshot(path)
    assert parsed.valid is False
    assert any(item.code == "E_SNAPSHOT_EMPTY" for item in parsed.fatal_errors)


def test_mask_implementations_are_deterministic():
    assert mask_value("redact", "secret") == "***"
    assert mask_value("email", "alice@example.com") == "a***@example.com"
    assert mask_value("last4", "123456") == "***3456"
    assert mask_value("hash", "secret") == mask_value("hash", "secret")
    assert mask_value("keep", "value") == "value"
