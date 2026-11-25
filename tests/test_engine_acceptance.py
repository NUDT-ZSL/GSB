from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from gradient_engine.cli import main as cli_main
from gradient_engine.engine import recover_store, run_to_completion, verify_replay
from gradient_engine.generator import generate_samples
from gradient_engine.storage import Store


ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture()
def workspace(tmp_path: Path) -> Path:
    samples = tmp_path / "samples"
    generate_samples(samples, seed=417)
    return tmp_path


def load_and_run(db_path: Path, samples: Path) -> Store:
    assert cli_main(["load", str(samples), "--db", str(db_path)]) == 0
    store = Store.connect(db_path)
    run_to_completion(store)
    return store


def test_scheduler_budget_memory_fault_and_spec(workspace: Path) -> None:
    samples = workspace / "samples"
    store = load_and_run(workspace / "fresh.db", samples)
    try:
        rounds = store.committed_rounds()
        assert rounds
        for round_row in rounds:
            started = store.conn.execute(
                "SELECT COUNT(*) AS n FROM round_candidates WHERE round_no=? AND outcome_code='SYNC_COMPLETED'",
                (round_row["round_no"],),
            ).fetchone()["n"]
            assert started <= 2

        budget = store.conn.execute(
            "SELECT COUNT(*) AS n FROM round_candidates WHERE outcome_code='BUDGET_EXHAUSTED'"
        ).fetchone()["n"]
        fault_lags = store.conn.execute(
            "SELECT COUNT(*) AS n FROM lag_records WHERE code='NODE_FAULT_LAG'"
        ).fetchone()["n"]
        memory_lags = store.conn.execute(
            "SELECT COUNT(*) AS n FROM lag_records WHERE code='MEMORY_LAG'"
        ).fetchone()["n"]
        takeovers = store.conn.execute("SELECT COUNT(*) AS n FROM takeovers").fetchone()["n"]
        assert budget > 0
        assert fault_lags > 0
        assert memory_lags > 0
        assert takeovers > 0

        n3 = store.conn.execute("SELECT memory_blocked FROM node_state WHERE node_id='n3'").fetchone()
        assert n3["memory_blocked"] == 1

        versions = [row["version"] for row in store.conn.execute("SELECT version FROM specs ORDER BY version")]
        assert versions == [1, 2]
        old_status = store.conn.execute("SELECT status FROM specs WHERE version=1").fetchone()["status"]
        new_status = store.conn.execute("SELECT status FROM specs WHERE version=2").fetchone()["status"]
        assert old_status == "retired"
        assert new_status == "active"

        old_entries = store.conn.execute(
            "SELECT COUNT(*) AS n FROM phase_completions WHERE spec_version=1"
        ).fetchone()["n"]
        assert old_entries > 0

        verification = verify_replay(store)
        assert verification["ok"] is True
    finally:
        store.close()


def test_crash_before_commit_recovers_identical_state(workspace: Path) -> None:
    samples = workspace / "samples"
    fresh_db = workspace / "fresh.db"
    recovered_db = workspace / "recovered.db"

    fresh_store = load_and_run(fresh_db, samples)
    fresh_trace = fresh_store.canonical_trace()
    fresh_status = fresh_store.status()
    fresh_ledger = fresh_store.ledger_summaries()
    fresh_store.close()

    assert cli_main(["load", str(samples), "--db", str(recovered_db)]) == 0
    proc = subprocess.run(
        [
            sys.executable, str(ROOT / "main.py"), "run",
            "--db", str(recovered_db),
            "--crash-before-commit-round", "3",
        ],
        cwd=ROOT,
        capture_output=True,
        text=True,
    )
    assert proc.returncode == 86

    crashed = Store.connect(recovered_db)
    assert [row["round_no"] for row in crashed.open_rounds()] == [3]
    recovered_rounds = recover_store(crashed)
    assert recovered_rounds == [3]
    run_to_completion(crashed, recover=False)

    assert crashed.canonical_trace() == fresh_trace
    assert crashed.ledger_summaries() == fresh_ledger
    recovered_status = crashed.status()
    for key in ("active_spec_version", "clock_watermark_ms", "completion_sets", "round_count"):
        assert recovered_status[key] == fresh_status[key]
    for node_id, node in fresh_status["nodes"].items():
        recovered_node = recovered_status["nodes"][node_id]
        for key in ("held_shards", "completed_shards", "completed_shards_by_spec", "memory_bytes", "outcome_codes"):
            assert recovered_node[key] == node[key]

    verification = verify_replay(crashed)
    assert verification["ok"] is True
    crashed.close()


def test_crash_after_commit_does_not_double_count_memory(workspace: Path) -> None:
    samples = workspace / "samples"
    db_path = workspace / "after_commit.db"
    assert cli_main(["load", str(samples), "--db", str(db_path)]) == 0
    proc = subprocess.run(
        [
            sys.executable, str(ROOT / "main.py"), "run",
            "--db", str(db_path),
            "--crash-after-commit-round", "4",
        ],
        cwd=ROOT,
        capture_output=True,
        text=True,
    )
    assert proc.returncode == 86

    store = Store.connect(db_path)
    committed_before = len(store.committed_rounds())
    memory_after_round_4 = store.conn.execute(
        "SELECT SUM(memory_bytes) AS total FROM memory_ledger WHERE round_no=4"
    ).fetchone()["total"]
    store.close()

    assert cli_main(["resume", "--db", str(db_path)]) == 0
    store = Store.connect(db_path)
    try:
        assert len(store.committed_rounds()) > committed_before
        duplicate_holdings = store.conn.execute(
            "SELECT spec_version,node_id,layer,shard_index,kind,COUNT(*) AS n "
            "FROM holdings GROUP BY spec_version,node_id,layer,shard_index,kind HAVING n>1"
        ).fetchall()
        assert duplicate_holdings == []
        ledger_round_4 = store.conn.execute(
            "SELECT SUM(memory_bytes) AS total FROM memory_ledger WHERE round_no=4"
        ).fetchone()["total"]
        assert ledger_round_4 == memory_after_round_4
        assert verify_replay(store)["ok"] is True
    finally:
        store.close()


def test_cli_status_and_verify_commands(workspace: Path, capsys: pytest.CaptureFixture[str]) -> None:
    samples = workspace / "samples"
    db = workspace / "cli.db"
    assert cli_main(["load", str(samples), "--db", str(db)]) == 0
    assert cli_main(["run", "--db", str(db)]) == 0
    capsys.readouterr()

    assert cli_main(["status", "--db", str(db)]) == 0
    status_output = json.loads(capsys.readouterr().out)
    assert status_output["ok"] is True
    assert "n1" in status_output["nodes"]

    assert cli_main(["verify", "--db", str(db)]) == 0
    verify_output = json.loads(capsys.readouterr().out)
    assert verify_output["ok"] is True


def test_verify_reports_first_ledger_divergence(workspace: Path) -> None:
    samples = workspace / "samples"
    store = load_and_run(workspace / "tamper.db", samples)
    try:
        with store.conn:
            store.conn.execute(
                "UPDATE memory_ledger SET memory_bytes=memory_bytes+1 "
                "WHERE rowid=(SELECT rowid FROM memory_ledger ORDER BY round_no, node_id LIMIT 1)"
            )
        result = verify_replay(store)
        assert result["ok"] is False
        assert result["reason"] == "memory_ledger_mismatch"
        assert result["first_mismatch_round"] == 1
        assert "first_mismatch_event_seq" in result
        assert result["expected_ledger_summary"]["digest"] != result["actual_ledger_summary"]["digest"]
    finally:
        store.close()
