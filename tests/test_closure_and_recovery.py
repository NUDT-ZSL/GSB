from __future__ import annotations

import json
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from account_verify.engine import Engine
from account_verify.errors import CrashInjected, LockError
from account_verify.locks import FileWriteLock
from account_verify.parsing import ParseBatch, parse_policy_v2_shard


def _full_adjudicated_digest(sample_dir: Path, state_dir: Path):
    engine = Engine(state_dir)
    try:
        engine.import_samples(sample_dir)
        engine.adjudicate(batch_size=2)
        return engine.ledger.compute_digest(), engine.ledger.list_user_decisions(), engine.ledger.list_tokens()
    finally:
        engine.close()


def test_incremental_closure_matches_full_readjudication(sample_dir: Path, tmp_path: Path, truth):
    incremental_state = tmp_path / "incremental"
    full_upgraded_state = tmp_path / "full-upgraded"

    engine = Engine(incremental_state)
    engine.import_samples(sample_dir)
    engine.adjudicate(batch_size=2)
    upgrade = engine.apply_policy_upgrade(sample_dir / "policy_upgrade_v3.json")
    closure = upgrade["closure"]
    engine.close()

    assert closure["trigger_source_policy_keys"] == truth["expected_upgrade_trigger_keys"]
    assert "u-gamma" in closure["rejudge_user_ids"]
    assert "u-direct" in closure["rejudge_user_ids"]

    fresh = Engine(full_upgraded_state)
    fresh.import_samples(sample_dir)
    upgrade_payload = json.loads((sample_dir / "policy_upgrade_v3.json").read_text(encoding="utf-8"))
    validation = ParseBatch()
    source_category = "policy_upgrade:policy_upgrade_v3.json"
    parse_policy_v2_shard(upgrade_payload, validation, source_category)
    fresh.bundle["policy_v2"].extend(validation.policy_v2)
    fresh.adjudicate(batch_size=2)
    full_digest = fresh.ledger.compute_digest()
    full_users = fresh.ledger.list_user_decisions()
    full_tokens = fresh.ledger.list_tokens()
    fresh.close()

    reopened = Engine(incremental_state)
    assert reopened.ledger.compute_digest() == full_digest
    incremental_users = reopened.ledger.list_user_decisions()
    incremental_tokens = reopened.ledger.list_tokens()
    assert incremental_users.keys() == full_users.keys()
    for user_id, decision in full_users.items():
        assert incremental_users[user_id]["status"] == decision["status"]
        assert incremental_users[user_id]["code"] == decision["code"]
        assert incremental_users[user_id].get("required_min_iterations") == decision.get("required_min_iterations")
    for token_id, row in full_tokens.items():
        assert incremental_tokens[token_id]["status"] == row["status"]
        assert incremental_tokens[token_id]["code"] == row["code"]
    gamma = reopened.ledger.list_user_decisions()["u-gamma"]
    strong = reopened.ledger.list_user_decisions()["u-strong"]
    assert gamma["status"] == "PENDING_MIGRATION"
    assert strong["status"] == "UP_TO_DATE"
    reopened.close()


@pytest.mark.parametrize("phase", ["before_snapshot", "temp_residue", "after_snapshot"])
def test_checkpoint_crash_recovery_matches_full_run(sample_dir: Path, tmp_path: Path, phase):
    crashed_state = tmp_path / f"crashed-{phase}"
    full_digest, _, _ = _full_adjudicated_digest(sample_dir, tmp_path / "full")

    engine = Engine(crashed_state)
    engine.import_samples(sample_dir)
    with pytest.raises(CrashInjected):
        engine.adjudicate(batch_size=2, crash_after_batch=1, crash_phase=phase)
    engine.close()

    recovered = Engine(crashed_state)
    if phase == "temp_residue":
        assert recovered.recovery["residue"]
        assert any(item["code"] == "W_CHECKPOINT_TEMP_RESIDUE" for item in recovered.recovery["warnings"])
    result = recovered.adjudicate(batch_size=2)
    assert result["resumed_from_batch_watermark"] == 2
    assert recovered.ledger.compute_digest() == full_digest
    verification = recovered.ledger.verify_digest()
    assert verification["valid"] is True
    recovered.close()


def test_corrupt_official_snapshot_is_restored_from_sqlite(adjudicated_engine: Engine):
    state_dir = adjudicated_engine.state_dir
    adjudicated_engine.close()
    snapshot = state_dir / "checkpoint.json"
    snapshot.write_text('{"watermark": "half-written', encoding="utf-8")

    recovered = Engine(state_dir)
    assert any(w["code"] == "W_CORRUPT_CHECKPOINT_SNAPSHOT" for w in recovered.recovery["warnings"])
    assert recovered.checkpoints.read_snapshot()["ledger_digest"] == recovered.ledger.latest_checkpoint()["ledger_digest"]
    assert recovered.ledger.verify_digest()["valid"]
    recovered.close()


def test_ledger_layout_version_rebuilds_old_database(tmp_path: Path):
    db_path = tmp_path / "ledger.sqlite3"
    conn = sqlite3.connect(db_path)
    conn.execute("PRAGMA user_version = 1")
    conn.execute("CREATE TABLE ancient_state(x TEXT)")
    conn.execute("INSERT INTO ancient_state VALUES ('must-disappear')")
    conn.commit()
    conn.close()

    engine = Engine(tmp_path)
    assert engine.ledger.layout_rebuilt is True
    version = engine.ledger.conn.execute("PRAGMA user_version").fetchone()[0]
    assert version == 3
    tables = {row[0] for row in engine.ledger.conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert "ancient_state" not in tables
    assert "users" in tables and "tokens" in tables and "checkpoints" in tables
    engine.close()


def test_lock_conflict_returns_e_lock(imported_engine: Engine):
    def try_engine():
        try:
            Engine(imported_engine.state_dir, lock_timeout_seconds=0)
            return None
        except LockError as exc:
            return exc.code

    with ThreadPoolExecutor(max_workers=1) as pool:
        code = pool.submit(try_engine).result(timeout=5)
    assert code == "E_LOCK"


def test_concurrent_writers_serialize_with_timeout(sample_dir: Path, tmp_path: Path):
    state = tmp_path / "state"
    setup = Engine(state)
    setup.import_samples(sample_dir)
    setup.close()

    def run_batch():
        engine = Engine(state, lock_timeout_seconds=5)
        try:
            return engine.adjudicate(batch_size=4)["final_batch_watermark"]
        finally:
            engine.close()

    with ThreadPoolExecutor(max_workers=3) as pool:
        results = list(pool.map(lambda _: run_batch(), range(3)))
    assert results == [20, 20, 20]


def test_direct_file_lock_conflict_code(tmp_path: Path):
    lock_path = tmp_path / "write.lock"
    holder = FileWriteLock(lock_path, timeout_seconds=0).acquire()
    with pytest.raises(LockError) as exc_info:
        FileWriteLock(lock_path, timeout_seconds=0).acquire()
    assert exc_info.value.code == "E_LOCK"
    holder.release()
