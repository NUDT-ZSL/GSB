from __future__ import annotations

from qrw.compiler import compile_templates
from qrw.errors import BusinessError
from qrw.expansion import expand_query
from qrw.migration import (
    downgrade_ir_to_v1,
    migrate_v1_to_v2,
    migrate_v2_to_v1,
    migrate_v2_to_v3,
)
from qrw.readers import read_template_file, read_template_text


def _candidate_signature(compiled, queries, config):
    return {
        query["query_id"]: [
            (item["candidate"], item["score_detail"], item["offset_map"])
            for item in expand_query(compiled, query, config)
        ]
        for query in queries
    }


def test_v1_to_v2_fills_and_preserves_candidates(
    samples, slot_types, queries, compiled_core, config
):
    parsed = read_template_file(samples / "v1.txt", slot_types, config)
    migration = migrate_v1_to_v2(parsed, config)
    fields = {item["field"] for item in migration["fill_ins"]}
    assert {"weight", "enabled", "max_depth"} <= fields
    assert any(item.startswith("slots.") for item in fields)
    roundtrip = read_template_text(
        migration["artifact"], "<v1_to_v2>", slot_types, config
    )
    compiled = compile_templates(roundtrip, config)
    assert _candidate_signature(compiled, queries, config) == _candidate_signature(
        compiled_core["v1"], queries, config
    )
    assert roundtrip.rules[0].pattern_text == parsed.rules[0].pattern_text
    assert roundtrip.rules[0].replacement_text == parsed.rules[0].replacement_text


def test_v2_to_v1_discards_are_explicit_and_roundtrip_equivalent(
    samples, slot_types, queries, compiled_core, config
):
    parsed = read_template_file(samples / "v2.txt", slot_types, config)
    migration = migrate_v2_to_v1(parsed)
    categories = {item["category"] for item in migration["discards"]}
    assert {"槽位", "weight", "enabled", "max_depth"} <= categories
    roundtrip = read_template_text(
        migration["artifact"], "<v2_to_v1>", slot_types, config
    )
    compiled = compile_templates(roundtrip, config)
    assert _candidate_signature(compiled, queries, config) == _candidate_signature(
        compiled_core["v2"], queries, config
    )
    assert roundtrip.rules[0].pattern_text == parsed.rules[0].pattern_text
    assert roundtrip.rules[0].replacement_text == parsed.rules[0].replacement_text


def test_v2_to_v3_upgrade(samples, slot_types, queries, compiled_core, config):
    parsed = read_template_file(samples / "v2.txt", slot_types, config)
    migration = migrate_v2_to_v3(parsed)
    roundtrip = read_template_text(
        migration["artifact"], "<v2_to_v3>", slot_types, config
    )
    compiled = compile_templates(roundtrip, config)
    assert _candidate_signature(compiled, queries, config) == _candidate_signature(
        compiled_core["v2"], queries, config
    )
    fields = {item["field"] for item in migration["fill_ins"]}
    assert {"conditions", "references"} <= fields


def test_v3_to_v1_drops_condition_and_reference(
    compiled_advanced,
):
    migration = downgrade_ir_to_v1(compiled_advanced["v3"])
    categories = {item["category"] for item in migration["discards"]}
    assert any("条件分支" in category for category in categories)
    assert any("引用" in category for category in categories)
    assert all(
        item["policy"] in ("drop_rule_explicitly", "drop_fragment_not_representable_in_v1")
        for item in migration["discards"]
    )

