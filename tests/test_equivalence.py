from __future__ import annotations

import copy

from qrw.equivalence import adjudicate


def test_core_dialects_are_semantically_equivalent(compiled_core, queries, config):
    report = adjudicate(compiled_core, queries, config)
    assert report["status"] == "EQUIVALENT"
    assert report["ok"] is True
    assert report["candidate_sets_equal"] is True
    assert report["candidate_order_equal"] is True
    assert report["unrecoverable_categories"] == []
    assert report["default_fill_sources"]
    assert all(
        item["source_line"] >= 1 and item["rule_id"]
        for item in report["default_fill_sources"]
    )


def test_advanced_v3_returns_semantic_drift(compiled_advanced, queries, config):
    report = adjudicate(compiled_advanced, queries, config)
    assert report["status"] == "DRIFT"
    assert report["code"] == "E_SEMANTIC_DRIFT"
    assert report["unrecoverable_categories"] == ["条件分支", "引用"]
    difference = report["first_difference"]
    assert difference["query_id"] == "q003"
    assert difference["field"] == "candidate"
    assert difference["candidate_index"] == 0
    assert difference["codepoint_offset"] >= 0


def test_slot_capability_drift_is_reported(compiled_core, queries, config):
    tampered = copy.deepcopy(compiled_core)
    for rule in tampered["v1"]["rules"]:
        for slot in rule["slots"]:
            if slot["name"] == "city":
                slot["type"] = "PROPERTY"
    report = adjudicate(tampered, queries, config)
    assert report["code"] == "E_SEMANTIC_DRIFT"
    assert "槽位" in report["unrecoverable_categories"]

