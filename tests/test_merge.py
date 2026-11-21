from __future__ import annotations

from pathlib import Path

from mvengine.definitions import parse_v1, parse_v2
from mvengine.merge import merge_definitions
from mvengine.package import load_package
from mvengine.util import canonical_json


def test_merge_is_bitwise_deterministic(sample_suite: dict[str, Path]) -> None:
    package = load_package(sample_suite["base"])
    first = merge_definitions(package.v1, package.v2)
    second = merge_definitions(package.v1, package.v2)
    assert canonical_json(first.payload) == canonical_json(second.payload)
    assert first.index_digest == second.index_digest
    assert first.index_digest == package.definition_digest


def test_same_clock_uses_v2_priority_and_keeps_audit(sample_suite: dict[str, Path]) -> None:
    package = load_package(sample_suite["base"])
    index = merge_definitions(package.v1, package.v2)
    east = next(item for item in index.decisions if item["view_name"] == "sales_east")
    assert east["selected"]["source_format"] == "v2"
    assert east["selected"]["logical_clock_ms"] == 100000
    assert east["reason"] == "same_clock_v2_format_priority"
    assert east["selected"]["source_priority"] > east["alternatives"][0]["source_priority"]


def test_v1_migration_and_tombstone_survive_merge(sample_suite: dict[str, Path]) -> None:
    package = load_package(sample_suite["base"])
    index = merge_definitions(package.v1, package.v2)
    views = index.views
    assert views["legacy_west"].status == "tombstoned"
    assert views["legacy_west"].tombstone_reason == "retired_in_v2"
    inventory = views["inventory_total"]
    assert inventory.source_format == "v1"
    assert inventory.generation == 2
    assert inventory.absolute_refresh_ms == 145000
    assert inventory.refresh_origin["representation"] == "relative_seconds"
    assert inventory.migration is not None
    assert inventory.migration["time"]["scale_ms"] == 1000
    assert inventory.migration["time"]["rounding"] == "floor"
    assert inventory.migration["generation"] == {"migrated": 2, "original": None}
    used = [
        item for item in index.migrations
        if item["view_name"] == "inventory_total" and item["used_in_index"]
    ]
    assert len(used) == 1


def test_due_rule_is_preserved_for_v1_migration(sample_suite: dict[str, Path]) -> None:
    package = load_package(sample_suite["base"])
    view = merge_definitions(package.v1, package.v2).views["inventory_total"]
    anchor_second = 100000 // 1000
    for clock in (0, 999, 100000, 144999, 145000, 145999, 146000, 500000):
        original_due = clock // 1000 >= anchor_second + 45
        migrated_due = clock >= view.absolute_refresh_ms
        assert original_due == migrated_due

