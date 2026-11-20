from __future__ import annotations

from pathlib import Path

from columnar_engine.engine import Engine

from conftest import cli, query


def test_hard_exit_after_commits_resumes_without_duplicate_accumulation(
    tmp_path: Path, sample_dir
) -> None:
    baseline = Engine(tmp_path / "baseline.db", tmp_path / "baseline-var").run_sample(
        sample_dir
    )

    db = tmp_path / "crash.db"
    work = tmp_path / "crash-var"
    first = cli(
        "run",
        "--sample",
        str(sample_dir),
        "--db",
        str(db),
        "--work-dir",
        str(work),
        "--crash-after-commits",
        "2",
    )
    assert first.returncode == 137
    partial = query(db, "SELECT COUNT(*) FROM shards WHERE status='COMMITTED'")[0][0]
    assert 0 < partial < 6

    resumed = cli(
        "resume",
        "--sample",
        str(sample_dir),
        "--db",
        str(db),
        "--work-dir",
        str(work),
    )
    assert resumed.returncode == 0, resumed.stderr
    import json

    summary = json.loads(resumed.stdout)["summary"]
    assert summary["status"] == "COMPLETE"
    assert summary["final_digest"] == baseline.final_digest
    assert summary["result"] == baseline.result
    assert query(db, "SELECT COUNT(*) FROM shards WHERE status='COMMITTED'")[0][0] == 6
    assert query(db, "SELECT COUNT(*) FROM commits")[0][0] == 12
    assert query(db, "SELECT COUNT(DISTINCT node_id || ':' || shard_id) FROM commits")[0][0] == 12


def test_hard_exit_after_ready_results_reuses_or_reexecutes_only_missing(
    tmp_path: Path, sample_dir
) -> None:
    baseline = Engine(tmp_path / "baseline.db", tmp_path / "baseline-var").run_sample(
        sample_dir
    )
    db = tmp_path / "crash-results.db"
    work = tmp_path / "crash-results-var"
    first = cli(
        "run",
        "--sample",
        str(sample_dir),
        "--db",
        str(db),
        "--work-dir",
        str(work),
        "--crash-after-results",
        "2",
    )
    assert first.returncode == 137
    resumed = cli(
        "resume",
        "--sample",
        str(sample_dir),
        "--db",
        str(db),
        "--work-dir",
        str(work),
    )
    import json

    summary = json.loads(resumed.stdout)["summary"]
    assert summary["status"] == "COMPLETE"
    assert summary["final_digest"] == baseline.final_digest
    assert query(db, "SELECT COUNT(*) FROM commits")[0][0] == 12

