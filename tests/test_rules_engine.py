from __future__ import annotations

import json

from audit_masker.engine import decide_event
from audit_masker.masking import DEFAULT_MASK_METHODS
from audit_masker.models import LayoutPolicy
from audit_masker.parsing import parse_rules_snapshot
from audit_masker.rules import build_unified_view

from conftest import as_event, event, snapshot, v2_rule, write_json


def test_same_generation_conflict_lists_source_values(tmp_path, samples_dir):
    parsed = parse_rules_snapshot(samples_dir / "rules_conflict.json")
    view = build_unified_view(
        parsed.records,
        parsed.layout,
        parsed.defaults,
    )
    assert len(view.conflicts) == 1
    conflict = view.conflicts[0]
    assert conflict.rule_name == "conflict-email"
    assert conflict.generation == 1
    assert len(conflict.field_values) == 2
    assert {item["source_format"] for item in conflict.field_values} == {
        "v1-pipe-line",
        "v2-json-object",
    }
    assert {item["mask"] for item in conflict.field_values} == {"email", "redact"}
    result = decide_event(as_event(event("c-1")), view)
    assert result.fields["email"].business_codes == ["E_RULE_CONFLICT"]
    assert result.fields["email"].decision == "unchanged"


def test_tombstone_and_later_generation_authority(tmp_path):
    doc = snapshot(
        clock_ms=1_000_000,
        v1=["r|payload.email|email|10", "dead|payload.note|redact|5"],
        v2=[
            v2_rule(
                "r",
                mask="hash",
                threshold=30,
                generation=2,
                effective_ms=1_000_500,
            ),
            v2_rule(
                "dead",
                field="payload.note",
                generation=3,
                effective_ms=1_001_000,
                tombstone=True,
            ),
        ],
    )
    parsed = parse_rules_snapshot(write_json(tmp_path / "rules.json", doc))
    view = build_unified_view(parsed.records, parsed.layout, parsed.defaults)
    assert view.rules_by_name["r"].state == "active"
    assert view.rules_by_name["r"].generation == 2
    assert view.rules_by_name["r"].mask == "hash"
    assert view.rules_by_name["dead"].state == "tombstoned"
    assert not any(rule.rule_name == "dead" for rule in view.active_rules)


def test_threshold_is_compared_before_generation_and_suppressed_rules_are_kept():
    from audit_masker.models import RuleRecord

    layout = LayoutPolicy(DEFAULT_MASK_METHODS)
    records = [
        RuleRecord(
            source_kind="snapshot",
            source_ref="s",
            record_index=1,
            source_format="v1-pipe-line",
            raw="strict|payload.phone|redact|60",
            rule_name="strict",
            field_path="payload.phone",
            mask="redact",
            threshold=60,
            effective_ms=1_000,
            generation=1,
            tombstone=False,
        ),
        RuleRecord(
            source_kind="snapshot",
            source_ref="s",
            record_index=2,
            source_format="v2-json-object",
            raw="newer",
            rule_name="newer",
            field_path="payload.phone",
            mask="last4",
            threshold=35,
            effective_ms=2_000,
            generation=9,
            tombstone=False,
        ),
    ]
    view = build_unified_view(records, layout, {})
    result = decide_event(
        as_event(event("p-1", payload={"phone": "13800138000"})),
        view,
    )
    decision = result.fields["phone"]
    assert decision.winning_rule_name == "strict"
    assert decision.winning_generation == 1
    assert decision.winning_threshold == 60
    assert decision.masked_value == "***"
    assert len(decision.matches) == 2
    assert len(decision.suppressed) == 1
    assert decision.suppressed[0]["rule_name"] == "newer"
    assert decision.suppressed[0]["suppressed_reason"] == "lower_threshold"


def test_generation_breaks_equal_threshold():
    from audit_masker.models import RuleRecord

    layout = LayoutPolicy(DEFAULT_MASK_METHODS)
    records = [
        RuleRecord(
            source_kind="snapshot",
            source_ref="s",
            record_index=1,
            source_format="v2-json-object",
            raw="old",
            rule_name="old",
            field_path="payload.email",
            mask="email",
            threshold=20,
            effective_ms=9_000,
            generation=1,
            tombstone=False,
        ),
        RuleRecord(
            source_kind="snapshot",
            source_ref="s",
            record_index=2,
            source_format="v2-json-object",
            raw="new",
            rule_name="new",
            field_path="payload.email",
            mask="hash",
            threshold=20,
            effective_ms=1_000,
            generation=4,
            tombstone=False,
        ),
    ]
    view = build_unified_view(records, layout, {})
    result = decide_event(as_event(event("g-1")), view)
    decision = result.fields["email"]
    assert decision.winning_rule_name == "new"
    assert decision.suppressed[0]["suppressed_reason"] == "lower_generation"


def test_category_default_gives_rule_name_and_generation(tmp_path):
    doc = snapshot(
        defaults={
            "contact": {
                "mask": "redact",
                "rule_name": "default:contact",
                "generation": 0,
                "fields": ["payload.email", "payload.note"],
            }
        }
    )
    parsed = parse_rules_snapshot(write_json(tmp_path / "rules.json", doc))
    view = build_unified_view(parsed.records, parsed.layout, parsed.defaults)
    result = decide_event(
        as_event(
            event(
                "d-1",
                payload={"email": "x@example.com", "note": "n", "request_id": "r"},
            )
        ),
        view,
    )
    assert result.fields["email"].basis == "category_default"
    assert result.fields["email"].winning_rule_name == "default:contact"
    assert result.fields["email"].winning_generation == 0
    assert result.fields["note"].masked_value == "***"
    assert result.fields["request_id"].decision == "unchanged"
