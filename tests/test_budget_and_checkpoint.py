from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from smuggling_engine.checkpoint import CheckpointLocked, CheckpointManager
from smuggling_engine.engine import Engine, EngineConfig

from conftest import load_sample


def test_message_budget_isolates_one_connection(standard_sample):
    _, rejects, groups, view = load_sample(standard_sample)
    engine = Engine(
        EngineConfig(
            concurrency=3,
            per_connection_timeout_ms=None,
            max_messages_per_connection=1,
            max_bytes_per_connection=None,
        )
    )
    report = engine.run(groups, view, rejected=rejects)
    by_id = {item.connection_id: item for item in report.connections}
    assert by_id["conn-01-clte"].verdict == "E_MSGS"
    assert by_id["conn-01-clte"].terminated
    assert by_id["conn-08-gap"].verdict == "E_MSGS"
    assert by_id["conn-05-clean"].verdict == "CLEAN"
    assert by_id["conn-05-clean"].status == "OK"
    assert "conn-01-clte" in report.terminated_connections
    assert "conn-05-clean" not in report.terminated_connections


def test_byte_budget_isolates_one_connection(standard_sample):
    _, rejects, groups, view = load_sample(standard_sample)
    engine = Engine(
        EngineConfig(
            concurrency=2,
            per_connection_timeout_ms=None,
            max_messages_per_connection=None,
            max_bytes_per_connection=80,
        )
    )
    report = engine.run(groups, view, rejected=rejects)
    by_id = {item.connection_id: item for item in report.connections}
    assert by_id["conn-01-clte"].verdict == "E_BYTES"
    assert by_id["conn-04-trailing"].verdict == "E_BYTES"
    assert by_id["conn-05-clean"].verdict == "CLEAN"
    assert by_id["conn-09-response"].verdict == "CLEAN"
    assert by_id["conn-05-clean"].terminated is False


def test_zero_wall_clock_budget_uses_monotonic_elapsed(standard_sample):
    _, rejects, groups, view = load_sample(standard_sample)
    report = Engine(
        EngineConfig(
            concurrency=4,
            per_connection_timeout_ms=0,
            max_messages_per_connection=None,
            max_bytes_per_connection=None,
        )
    ).run(groups, view, rejected=rejects)
    assert len(report.terminated_connections) == len(groups)
    assert {item.verdict for item in report.connections} == {"E_TIMEOUT"}
    assert all(item.termination_reason == "E_TIMEOUT" for item in report.connections)


def test_checkpoint_resume_reuses_completed_prefix_and_matches_full(standard_sample):
    _, rejects, groups, view = load_sample(standard_sample)
    engine = Engine(EngineConfig(concurrency=2, per_connection_timeout_ms=None))
    checkpoint_dir = Path(standard_sample["directory"]) / "checkpoint-prefix"
    manager = CheckpointManager(checkpoint_dir)
    first = engine.run_with_checkpoint(
        groups[:3], view, manager, run_identity="sample-identity", rejected=rejects
    )
    assert len(first.connections) == 3
    resumed = engine.run_with_checkpoint(
        groups, view, manager, run_identity="sample-identity", rejected=rejects
    )
    full = engine.run(groups, view, rejected=rejects)
    resumed_meta = resumed.metadata["checkpoint"]
    assert set(resumed_meta["reused_connections"]) == {
        "conn-01-clte",
        "conn-02-duplicate-conflict",
        "conn-03-chunk-hex",
    }
    assert resumed.deterministic_dict() == full.deterministic_dict()
    assert not list(checkpoint_dir.glob("*.tmp"))
    assert not list(checkpoint_dir.glob(".*.tmp"))


def test_checkpoint_half_write_is_detected_and_ignored(standard_sample):
    _, rejects, groups, view = load_sample(standard_sample)
    engine = Engine(EngineConfig(concurrency=1, per_connection_timeout_ms=None))
    checkpoint_dir = Path(standard_sample["directory"]) / "checkpoint-half-write"
    manager = CheckpointManager(checkpoint_dir)
    engine.run_with_checkpoint(
        groups[:2], view, manager, run_identity="half-write", rejected=rejects
    )
    orphan = checkpoint_dir / "checkpoint-99999999.json"
    orphan.write_text(
        json.dumps(
            {
                "schema_version": manager.schema_version,
                "generation": 99999999,
                "checkpoint_id": "orphan",
                "payload_sha256": "0" * 64,
                "payload": {},
            }
        ),
        encoding="utf-8",
    )
    loaded = manager.load(
        expected_config_fingerprint=engine.config.fingerprint(),
        expected_rules_version=view.version,
        expected_merge_policy_version=view.merge_policy_version,
    )
    assert loaded.payload is not None
    assert loaded.recovered
    assert any(item.startswith("orphan_snapshot:") for item in loaded.residue)
    assert loaded.generation < 99999999


def test_corrupt_latest_database_row_falls_back_to_previous_complete(
    standard_sample,
):
    _, rejects, groups, view = load_sample(standard_sample)
    engine = Engine(EngineConfig(concurrency=1, per_connection_timeout_ms=None))
    checkpoint_dir = Path(standard_sample["directory"]) / "checkpoint-fallback"
    manager = CheckpointManager(checkpoint_dir)
    engine.run_with_checkpoint(
        groups[:2], view, manager, run_identity="fallback", rejected=rejects
    )
    connection = sqlite3.connect(str(manager.database_path))
    try:
        latest = connection.execute(
            "SELECT MAX(generation) FROM checkpoints"
        ).fetchone()[0]
        connection.execute(
            "UPDATE checkpoints SET payload_sha256 = ? WHERE generation = ?",
            ("0" * 64, latest),
        )
        connection.commit()
    finally:
        connection.close()
    loaded = manager.load(
        expected_config_fingerprint=engine.config.fingerprint(),
        expected_rules_version=view.version,
        expected_merge_policy_version=view.merge_policy_version,
    )
    assert loaded.payload is not None
    assert loaded.generation == latest - 1
    assert loaded.recovered
    assert any(
        item.startswith("payload_hash_mismatch:") for item in loaded.residue
    )


def test_checkpoint_lock_returns_e_lock(standard_sample):
    checkpoint_dir = Path(standard_sample["directory"]) / "checkpoint-lock"
    manager = CheckpointManager(checkpoint_dir)
    with manager.exclusive():
        with pytest.raises(CheckpointLocked) as error:
            manager.load()
    assert error.value.code == "E_LOCK"


def test_checkpoint_schema_and_rule_version_changes_force_rebuild(standard_sample):
    _, rejects, groups, view = load_sample(standard_sample)
    engine = Engine(EngineConfig(concurrency=1, per_connection_timeout_ms=None))
    checkpoint_dir = Path(standard_sample["directory"]) / "checkpoint-version"
    manager = CheckpointManager(checkpoint_dir)
    engine.run_with_checkpoint(
        groups[:2], view, manager, run_identity="versions", rejected=rejects
    )
    changed_rules = manager.load(
        expected_config_fingerprint=engine.config.fingerprint(),
        expected_rules_version="different-rules-version",
        expected_merge_policy_version=view.merge_policy_version,
    )
    assert changed_rules.payload is None
    assert changed_rules.reason == "E_CHECKPOINT_RULESET_CHANGED"
    changed_schema = CheckpointManager(checkpoint_dir, schema_version=999)
    loaded = changed_schema.load(
        expected_config_fingerprint=engine.config.fingerprint(),
        expected_rules_version=view.version,
        expected_merge_policy_version=view.merge_policy_version,
    )
    assert loaded.payload is None
    assert loaded.reason == "E_CHECKPOINT_SCHEMA_CHANGED"

