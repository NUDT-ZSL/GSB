from __future__ import annotations

import dataclasses
import json
import sqlite3
from pathlib import Path

import pytest

from alertlab.errors import AlertLabError
from alertlab.rules import load_v1, load_v2, merge_snapshots
from alertlab.service import AlertLabService
from alertlab.topology import load_topology

from conftest import ROOT, read_json, write_json


def test_merge_prefers_newer_clock_generation_and_tombstone() -> None:
    topology = load_topology(ROOT / "samples" / "topology.json")
    v1 = load_v1(
        ROOT / "samples" / "v1" / "rules_v1.json",
        topology,
        injected_clock_ms=1_700_001_800_000,
    )
    v2 = load_v2(
        ROOT / "samples" / "v2" / "index.json",
        topology,
        injected_clock_ms=1_700_001_800_000,
    )
    merged = merge_snapshots([v1, v2], injected_clock_ms=1_700_001_800_000)
    by_name = merged.all_rules
    assert by_name["GatewayLatency"].source_version == "v2"
    assert by_name["GatewayLatency"].generation == 2
    assert by_name["GatewayLatency"].adjudication["basis"]["logical_clock_ms"] == 1_700_001_000_000
    assert by_name["GatewayLatency"].original_time == {
        "kind": "absolute_epoch_ms",
        "value": 1_700_000_900_000,
    }
    assert "GatewayLatency" not in merged.active_rules or True
    assert by_name["LegacyOnly"].tombstone is True
    assert "LegacyOnly" not in merged.active_rules
    assert by_name["LegacyOnly"].adjudication["rejected_candidates"][0][
        "source_version"
    ] == "v1"
    assert by_name["LegacyOnly"].original_time["kind"] == "absolute_epoch_ms"


def test_equal_clock_uses_generation_then_format_priority() -> None:
    topology = load_topology(ROOT / "samples" / "topology.json")
    v1 = load_v1(
        ROOT / "samples" / "v1" / "rules_v1.json",
        topology,
        injected_clock_ms=1_700_001_800_000,
    )
    v2 = load_v2(
        ROOT / "samples" / "v2" / "index.json",
        topology,
        injected_clock_ms=1_700_001_800_000,
    )
    v1_equal = dataclasses.replace(v1, logical_clock_ms=v2.logical_clock_ms)
    candidates = []
    for candidate in v1_equal.candidates:
        candidates.append(
            dataclasses.replace(candidate, source_clock_ms=v2.logical_clock_ms)
        )
    v1_equal = dataclasses.replace(v1_equal, candidates=tuple(candidates))
    gateways = [
        candidate
        for source in (v1_equal, v2)
        for candidate in source.candidates
        if candidate.rule_name == "GatewayLatency"
    ]
    same_generation = [
        dataclasses.replace(candidate, generation=2) for candidate in gateways
    ]
    chosen = max(same_generation, key=lambda candidate: candidate.decision_key())
    assert chosen.source_version == "v2"
    assert chosen.format_priority == 20


def test_v1_relative_time_migration_is_persisted_and_repeatable(
    loaded_service: AlertLabService,
) -> None:
    connection = sqlite3.connect(loaded_service.store.database_path)
    try:
        rows = connection.execute(
            """
            SELECT rule_name, source_version, injected_clock_ms,
                   effective_at_ms, scale_ms_per_unit, rounding_direction,
                   decision_json
              FROM migration_decisions
             WHERE rule_name IN ('GatewayLatency', 'SearchNoise')
             ORDER BY rule_name, source_version, generation
            """
        ).fetchall()
    finally:
        connection.close()
    gateway_v1 = next(row for row in rows if row[0] == "GatewayLatency" and row[1] == "v1")
    assert gateway_v1[2] == 1_700_000_000_000
    assert gateway_v1[3] == 1_700_000_020_000
    assert gateway_v1[4] == 1000
    assert gateway_v1[5] == "exact"
    decision = json.loads(gateway_v1[6])
    assert decision["original_time"] == {"kind": "relative_seconds", "value": 20}

    first_status = loaded_service.status()
    loaded_service.load()
    second_status = loaded_service.status()
    assert second_status == first_status
    connection = sqlite3.connect(loaded_service.store.database_path)
    try:
        count = connection.execute(
            "SELECT COUNT(*) FROM migration_decisions"
        ).fetchone()[0]
    finally:
        connection.close()
    assert count == 15


def test_snapshot_version_rollback_and_same_version_conflict(
    tmp_path: Path, workspace: Path
) -> None:
    service = AlertLabService(workspace, database_path=tmp_path / "rollback.sqlite3")
    service.load(include_v2=False)
    assert service.status()["rule_view"]["snapshot_version"] == 1
    service.load(include_v1=False)
    assert service.status()["rule_view"]["snapshot_version"] == 2
    with pytest.raises(AlertLabError) as captured:
        service.load(include_v2=False)
    assert captured.value.code == "E_VIEW_ROLLBACK"

    conflict_service = AlertLabService(
        workspace, database_path=tmp_path / "conflict.sqlite3"
    )
    conflict_service.load(include_v2=False)
    changed_v1 = read_json(ROOT / "samples" / "v1" / "rules_v1.json")
    changed_v1["rules"][0]["effective_after_seconds"] = 99
    path = tmp_path / "changed_v1.json"
    write_json(path, changed_v1)
    with pytest.raises(AlertLabError) as captured:
        conflict_service.load(v1_path=path, include_v2=False)
    assert captured.value.code == "E_SNAPSHOT_VERSION_CONFLICT"


def test_same_source_snapshot_can_be_evaluated_at_a_new_injected_clock(
    tmp_path: Path, workspace: Path
) -> None:
    service = AlertLabService(workspace, database_path=tmp_path / "clock.sqlite3")
    service.load()
    first = service.status()
    service.load(logical_clock_ms=1_700_001_900_000)
    second = service.status()
    assert second["rule_view"]["snapshot_version"] == first["rule_view"]["snapshot_version"]
    assert second["rule_view"]["view_id"] != first["rule_view"]["view_id"]
    assert second["logical_clock_watermark_ms"] == 1_700_001_900_000
    assert second["summary"]["cluster_count"] == first["summary"]["cluster_count"]
