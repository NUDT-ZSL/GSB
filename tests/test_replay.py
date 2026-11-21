from __future__ import annotations

import sqlite3
import json
import shutil
from collections import Counter
from pathlib import Path

import pytest

from walrec.engine import Engine
from walrec.errors import WalRecError
from walrec.canonical import canonical_json, tx_digest

from conftest import BUDGET, NORMAL


def test_cross_version_replay_is_row_identical_and_detects_injected_difference(
    tmp_path: Path,
) -> None:
    full = Engine(tmp_path / "full.db")
    full.load(NORMAL, checkpoint_gen=1)
    full_result = full.apply(NORMAL, no_auto_checkpoint=True)
    assert full_result["ok"]
    assert full_result["status"]["applied_watermark"] == 17

    incremental = Engine(tmp_path / "incremental.db")
    incremental.load(NORMAL, checkpoint_gen=2)
    incremental_result = incremental.apply(NORMAL, no_auto_checkpoint=True)
    assert incremental_result["ok"]
    assert incremental_result["applied"] == list(range(6, 18))

    comparison = incremental.compare(tmp_path / "full.db")
    assert comparison["ok"]
    assert comparison["different_row_count"] == 0

    with sqlite3.connect(tmp_path / "incremental.db") as connection:
        connection.execute(
            """
            UPDATE data_rows
            SET payload_json = '{"balance":999}', version = 99, source_tx = 99
            WHERE logical_table = 'accounts' AND row_key = 'eve'
            """
        )
        connection.commit()
    injected = incremental.compare(tmp_path / "full.db")
    assert not injected["ok"]
    assert injected["different_row_count"] >= 1
    assert injected["differences"][0]["txid"] == 99


def test_budget_abandonment_is_recorded_and_does_not_truncate_started_work(
    tmp_path: Path,
) -> None:
    engine = Engine(tmp_path / "budget.db")
    engine.load(BUDGET, checkpoint_gen=1)
    result = engine.apply(BUDGET)
    statuses = Counter(outcome["status"] for outcome in result["outcomes"])
    assert statuses["not_started_budget"] > 0
    assert statuses["applied"] == 17
    assert result["status"]["applied_watermark"] == 17
    assert result["ok"]
    for outcome in result["outcomes"]:
        assert outcome["round_id"].startswith("round-")
        if outcome["status"] == "not_started_budget":
            assert outcome["started_at_ms"] is None
            assert outcome["reason"] == "round_budget_exhausted"

    with sqlite3.connect(tmp_path / "budget.db") as connection:
        rows = connection.execute(
            """
            SELECT round_id, SUM(CASE WHEN status = 'applied' THEN 1 ELSE 0 END)
            FROM tx_outcomes
            GROUP BY round_id
            """
        ).fetchall()
    assert max(int(row[1]) for row in rows) <= 2


def test_exhausted_round_with_no_fitting_transaction_stops(tmp_path: Path) -> None:
    engine = Engine(tmp_path / "hard-budget.db")
    engine.load(NORMAL, checkpoint_gen=1)
    result = engine.apply(
        NORMAL,
        no_auto_checkpoint=True,
        policy_overrides={"round_budget_ms": 3},
    )
    assert not result["ok"]
    assert result["applied"] == []
    assert result["status"]["applied_watermark"] == 0
    statuses = Counter(outcome["status"] for outcome in result["outcomes"])
    assert statuses == {"not_started_budget": 17}


def test_started_failure_and_active_abandon_are_distinct(tmp_path: Path) -> None:
    engine = Engine(tmp_path / "failure.db")
    engine.load(NORMAL, checkpoint_gen=1)
    result = engine.apply(
        NORMAL,
        policy_overrides={"inject_failure_tx_ids": (3,)},
    )
    assert not result["ok"]
    assert result["applied"] == [1, 2]
    assert result["status"]["applied_watermark"] == 2
    by_tx = {}
    for outcome in result["outcomes"]:
        by_tx.setdefault(outcome["txid"], []).append(outcome["status"])
    assert by_tx[3] == ["failed_after_start"]
    assert "actively_abandoned" in by_tx[4]
    assert all("applied" in values for values in (by_tx[1], by_tx[2]))

    resumed = engine.resume(NORMAL, no_auto_checkpoint=True)
    assert resumed["ok"]
    assert resumed["status"]["applied_watermark"] == 17
    assert engine.verify(NORMAL)["ok"]


def test_clock_rollback_is_rejected_without_state_change(tmp_path: Path) -> None:
    engine = Engine(tmp_path / "clock.db")
    engine.load(NORMAL)
    before = engine.snapshot()
    with pytest.raises(WalRecError) as captured:
        engine.apply(NORMAL, clock_ms=1400)
    assert captured.value.code == "E_CLOCK_ROLLBACK"
    assert engine.snapshot() == before


def test_repeat_apply_is_idempotent(tmp_path: Path) -> None:
    engine = Engine(tmp_path / "idempotent.db")
    engine.load(NORMAL)
    first = engine.apply(NORMAL)
    assert first["ok"]
    before = engine.snapshot()
    second = engine.apply(NORMAL)
    assert second["ok"]
    assert second["applied"] == []
    assert second["outcomes"] == []
    assert second["status"] == first["status"]
    assert engine.snapshot() == before


def test_wal_may_contain_only_transactions_after_checkpoint(tmp_path: Path) -> None:
    incremental_sample = tmp_path / "incremental-wal"
    shutil.copytree(NORMAL, incremental_sample)
    records = []
    for path in sorted((NORMAL / "wal").glob("wal-*.jsonl")):
        with path.open("r", encoding="utf-8") as handle:
            records.extend(json.loads(line) for line in handle)
    tail = [record for record in records if record["logical_tx_id"] >= 6]
    previous_hash = tail[0]["prev_tx_hash"]
    for record in tail:
        record["segment_no"] = 1
        record["prev_tx_hash"] = previous_hash
        record["tx_hash"] = tx_digest(record)
        previous_hash = record["tx_hash"]
    wal_dir = incremental_sample / "wal"
    shutil.rmtree(wal_dir)
    wal_dir.mkdir()
    with (wal_dir / "wal-0001.jsonl").open("w", encoding="utf-8") as handle:
        for record in tail:
            handle.write(canonical_json(record) + "\n")

    incremental = Engine(tmp_path / "incremental-wal.db")
    incremental.load(incremental_sample, checkpoint_gen=2)
    result = incremental.apply(incremental_sample, no_auto_checkpoint=True)
    assert result["ok"]
    assert result["applied"] == list(range(6, 18))
    assert incremental.verify(incremental_sample)["ok"]

    reference = Engine(tmp_path / "incremental-reference.db")
    reference.load(NORMAL)
    reference.apply(NORMAL, no_auto_checkpoint=True)
    assert incremental.compare(tmp_path / "incremental-reference.db")["ok"]
