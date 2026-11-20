from __future__ import annotations

from pathlib import Path

from columnar_engine.engine import Engine

from conftest import query, values_by_column


def test_parallel_execution_and_repeatability(tmp_path: Path, sample_dir) -> None:
    db = tmp_path / "engine.db"
    work = tmp_path / "var"
    engine = Engine(db, work)
    first = engine.run_sample(sample_dir)
    assert first.status == "COMPLETE"
    assert first.shard_count == 6
    assert first.shard_status_counts == {"COMMITTED": 6}
    assert len(first.scheduler["waves"]) == 2
    assert [wave["started"] for wave in first.scheduler["waves"]] == [
        [0, 1, 2],
        [3, 4, 5],
    ]
    assert values_by_column(first.result, "category") == [1, 2]
    assert values_by_column(first.result, "total_sum") == [8.0, 8.0]
    assert values_by_column(first.result, "fixed_total") == [800, 800]
    assert first.final_digest

    second = engine.run_sample(sample_dir)
    assert second.status == "COMPLETE"
    assert second.final_digest == first.final_digest
    assert second.scheduler["committed_this_call"] == []
    assert query(db, "SELECT COUNT(*) FROM shards WHERE status='COMMITTED'")[0][0] == 6


def test_independent_runs_are_bitwise_identical(tmp_path: Path, sample_dir) -> None:
    first = Engine(tmp_path / "a.db", tmp_path / "a-var").run_sample(sample_dir)
    second = Engine(tmp_path / "b.db", tmp_path / "b-var").run_sample(sample_dir)
    assert first.final_digest == second.final_digest
    assert first.result == second.result


def test_committed_state_and_reader_snapshot(tmp_path: Path, sample_dir) -> None:
    engine = Engine(tmp_path / "engine.db", tmp_path / "var")
    summary = engine.run_sample(sample_dir)
    snapshot = engine.snapshot(summary.run_id)
    assert snapshot["run"]["status"] == "COMPLETE"
    assert len(snapshot["shards"]) == 6
    assert all(shard["status"] == "COMMITTED" for shard in snapshot["shards"])
    assert {state["node_id"] for state in snapshot["node_states"]} == {"window1", "agg1"}
    assert all(state["watermark"] == 5 for state in snapshot["node_states"])
    assert snapshot["errors"] == []

