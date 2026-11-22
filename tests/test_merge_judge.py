from __future__ import annotations

import json
import sqlite3

import pytest

from log_retention.config import Config
from log_retention.engine import Engine
from log_retention.errors import ArchiveError
from log_retention.fixedpoint import compute_margin
from log_retention.models import (
    InstanceReplayState,
    NormalizedInput,
    Policy,
    Transaction,
)
from log_retention.policy import build_policy_view


def _loaded(config: Config) -> Engine:
    engine = Engine(config)
    engine.load()
    return engine


def test_merge_prefers_v2_on_equal_clock_and_builds_conflict(config: Config) -> None:
    engine = _loaded(config)
    try:
        merged = engine.merge()
        assert merged["conflict_count"] == 1
        with sqlite3.connect(config.ledger_db) as conn:
            conn.row_factory = sqlite3.Row
            v1 = conn.execute(
                "SELECT * FROM view_policies WHERE policy_key = 'v1:v1-a'"
            ).fetchone()
            v2 = conn.execute(
                "SELECT * FROM view_policies WHERE policy_key = 'v2:v2-a-g1'"
            ).fetchone()
            assert v1["selected"] == 0
            assert v2["selected"] == 1
            decision = json.loads(v2["merge_decision_json"])
            assert decision["reason"] == "cross_version_latest_source"
            assert decision["winner"] == "v2:v2-a-g1"
            merged_from = json.loads(v2["merged_from_json"])
            assert {item["source_version"] for item in merged_from} == {"v1", "v2"}
            v1_origin = next(
                item for item in merged_from if item["source_version"] == "v1"
            )
            assert v1_origin["original_time_representation"]["kind"] == (
                "relative_seconds"
            )
            assert v1_origin["original_time_representation"]["value"] == 8
            conflict = conn.execute(
                "SELECT * FROM conflict_records WHERE segment_id = 3 "
                "AND resolved_in_rebuild = 0"
            ).fetchone()
            assert conflict["winning_policy_key"] == "v2:v2-b-seg3-new"
            assert json.loads(conflict["losing_policy_keys_json"]) == [
                "v2:v2-b-seg3-old"
            ]
            assert conflict["common_ancestor_policy_key"] == "v2:v2-b-root"
    finally:
        engine.close()


def test_cross_version_root_policies_use_latest_clock() -> None:
    v1 = Policy(
        policy_id="root",
        source_version="v1",
        instance_id="replica-x",
        segment_id=None,
        retention_generation=1,
        effective_at_ms=9000,
        relative_seconds=8,
        absolute_cutoff_ms=None,
        required_min_catchup_margin=0,
        parent_policy_id=None,
        tombstone=False,
        retention_window="v1-root",
        source_ref="test:v1",
        source_priority=10,
        raw={},
    )
    v2 = Policy(
        policy_id="root",
        source_version="v2",
        instance_id="replica-x",
        segment_id=None,
        retention_generation=2,
        effective_at_ms=9500,
        relative_seconds=None,
        absolute_cutoff_ms=11000,
        required_min_catchup_margin=0,
        parent_policy_id=None,
        tombstone=False,
        retention_window="v2-root",
        source_ref="test:v2",
        source_priority=20,
        raw={},
    )
    normalized = NormalizedInput(
        transactions=[Transaction(1, 1000, 100, 1)],
        replay_events=[],
        replay_states={
            "replica-x": InstanceReplayState(
                instance_id="replica-x",
                latest_txn_id=1,
                latest_replay_ms=1000,
                result="active",
                tombstone=False,
                event_count=1,
            )
        },
        policies=[v1, v2],
        segment_ids=[1],
        fingerprint="test",
    )
    view = build_policy_view(normalized, clock_ms=20000)
    assert view.nodes["v1:root"].selected is False
    assert view.nodes["v2:root"].selected is True
    assert view.segment_selected[1]["replica-x"].key == "v2:root"
    assert {item["source_version"] for item in view.nodes["v2:root"].merged_from} == {
        "v1",
        "v2",
    }
    assert view.conflicts == []


def test_duplicate_policy_ids_across_generations_are_supported(config: Config) -> None:
    shard = config.v2_shards[0]
    document = json.loads(shard.read_text(encoding="utf-8"))
    for policy in document["policies"]:
        if policy["policy_id"] == "v2-a-g1":
            policy["policy_id"] = "v1-a"
        if policy["policy_id"] == "v2-a-g2":
            policy["parent_policy_id"] = "v1-a"
    shard.write_text(json.dumps(document, sort_keys=True), encoding="utf-8")
    with sqlite3.connect(config.v2_meta_db) as conn:
        conn.execute(
            "UPDATE policy_meta SET policy_id = 'v1-a' WHERE policy_id = 'v2-a-g1'"
        )
        conn.execute(
            "UPDATE policy_meta SET parent_policy_id = 'v1-a' "
            "WHERE policy_id = 'v2-a-g2'"
        )
        conn.commit()
    engine = Engine(config)
    try:
        assert engine.load()["policies"] == 10
        merged = engine.merge()
        assert merged["conflict_count"] == 1
        with sqlite3.connect(config.ledger_db) as conn:
            selected = conn.execute(
                "SELECT source_version FROM view_policies "
                "WHERE source_policy_id = 'v1-a' AND selected = 1"
            ).fetchall()
            assert selected == [("v2",)]
    finally:
        engine.close()


def test_judge_produces_three_states_and_path_evidence(config: Config) -> None:
    engine = _loaded(config)
    try:
        engine.merge()
        result = engine.judge()
        assert result["status"] == "committed"
        decisions = {row["segment_id"]: row for row in result["segments"]}
        assert decisions[1]["decision"] == "ARCHIVE"
        assert decisions[2]["decision"] == "ALERT"
        assert decisions[2]["reason_code"] == "E_PRECISION_SENSITIVE_BOUNDARY"
        assert decisions[3]["decision"] == "ALERT"
        assert decisions[3]["reason_code"] == "E_CONFLICT_PENDING_REBUILD"
        assert decisions[4]["decision"] == "RETAIN"
        assert decisions[5]["decision"] == "RETAIN"
        assert decisions[3]["fixed_margin_units_total"] == 6_195_175
        paths = decisions[3]["evidence"]["retention_paths"]
        assert [item["instance_id"] for item in paths] == [
            "replica-a",
            "replica-b",
            "replica-c",
        ]
        assert all(item["path"][0] == "segment:3" for item in paths)
        assert all(item["path"][1].startswith("instance:") for item in paths)
        assert all(item["path"][2].startswith("policy:") for item in paths)
        assert decisions[3]["evidence"]["rejected_paths"][0]["policy_id"] == (
            "v2-b-seg3-old"
        )
        assert decisions[3]["precision_sensitive"] is True
    finally:
        engine.close()


def test_fixedpoint_margin_is_idempotent_at_same_clock(config: Config) -> None:
    engine = _loaded(config)
    try:
        engine.merge()
        first = engine.judge()
        second = engine.judge()
        assert first["view_version"] == second["view_version"]
        assert first["instance_retention"] == second["instance_retention"]
        margins = {row["instance_id"]: row for row in first["instance_retention"]}
        assert margins["replica-b"]["lag_bytes"] == 10801
        assert margins["replica-b"]["effective_margin_units"] == 4_000_370
        assert margins["replica-b"]["remainder_units"] == 1000
        assert margins["replica-c"]["remainder_units"] == 5000
        with sqlite3.connect(config.ledger_db) as conn:
            windows = conn.execute(
                "SELECT * FROM retention_windows WHERE instance_id = 'replica-b' "
                "AND logical_clock_ms = 20000"
            ).fetchall()
            assert len(windows) == 1
            calculations = conn.execute(
                "SELECT COUNT(*) FROM op_calculations WHERE state = 'committed'"
            ).fetchone()[0]
            assert calculations == 3
    finally:
        engine.close()


def test_catchup_remainder_carries_into_next_window(config: Config) -> None:
    engine = _loaded(config)
    try:
        engine.merge()
        engine.judge()
        later = engine.judge(requested_clock_ms=21000)
        margins = {row["instance_id"]: row for row in later["instance_retention"]}
        assert margins["replica-b"]["carry_in_units"] == 1000
        assert margins["replica-b"]["remainder_units"] == 2000
        assert margins["replica-b"]["effective_margin_units"] == 4_000_370
        with sqlite3.connect(config.ledger_db) as conn:
            rows = conn.execute(
                "SELECT logical_clock_ms, carry_in_units, remainder_units "
                "FROM retention_windows WHERE instance_id = 'replica-b' "
                "ORDER BY logical_clock_ms"
            ).fetchall()
            assert rows == [(20000, 0, 1000), (21000, 1000, 2000)]
    finally:
        engine.close()


def test_clock_rollback_exceeding_tolerance_is_atomic(config: Config) -> None:
    engine = _loaded(config)
    try:
        engine.merge()
        engine.judge()
        before = engine.status()
        with pytest.raises(ArchiveError) as caught:
            engine.judge(requested_clock_ms=19000)
        assert caught.value.code == "E_CLOCK_ROLLBACK"
        after = engine.status()
        assert before["view_version"] == after["view_version"]
        assert before["content_hash"] == after["content_hash"]
        with sqlite3.connect(config.ledger_db) as conn:
            ops = conn.execute(
                "SELECT COUNT(*) FROM operations WHERE requested_clock_ms = 19000"
            ).fetchone()[0]
            assert ops == 0
    finally:
        engine.close()


def test_rebuild_at_new_clock_recomputes_carry_and_verifies(config: Config) -> None:
    engine = _loaded(config)
    try:
        engine.merge()
        engine.judge()
        rebuilt = engine.rebuild(requested_clock_ms=21000)
        assert rebuilt["status"] == "committed"
        assert rebuilt["logical_clock_ms"] == 21000
        assert rebuilt["clean_equivalence"]["all_equal"] is True
        with sqlite3.connect(config.ledger_db) as conn:
            windows = conn.execute(
                "SELECT carry_in_units, remainder_units FROM retention_windows "
                "WHERE instance_id = 'replica-b' AND logical_clock_ms = 21000"
            ).fetchone()
            assert windows == (1000, 2000)
        assert engine.verify()["status"] == "ok"
    finally:
        engine.close()


def test_fixedpoint_helper_carries_fractional_unit() -> None:
    first = compute_margin(
        lag_bytes=7,
        replay_rate_bytes_per_sec=3,
        scale_units_per_ms=1000,
        declared_min_units=0,
    )
    second = compute_margin(
        lag_bytes=7,
        replay_rate_bytes_per_sec=3,
        scale_units_per_ms=1000,
        declared_min_units=0,
        carry_in_units=first.remainder_units,
    )
    assert first.computed_units == 2_333_333
    assert first.remainder_units == 1
    assert second.carry_in_units == 1
    assert second.remainder_units == 2
    assert second.computed_units == first.computed_units
