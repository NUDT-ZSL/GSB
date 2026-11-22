from __future__ import annotations

import json
import sqlite3

from conftest import cli

from log_retention.config import Config
from log_retention.engine import Engine


def _prepare(config: Config) -> None:
    assert cli(config, "load").returncode == 0
    assert cli(config, "merge").returncode == 0


def test_rebuild_tombstones_loser_and_preserves_history(config: Config) -> None:
    engine = Engine(config)
    try:
        engine.load()
        engine.merge()
        judged = engine.judge()
        rebuilt = engine.rebuild()
        assert rebuilt["status"] == "committed"
        assert rebuilt["invalidated_policies"] == ["v2:v2-b-seg3-old"]
        assert rebuilt["rebuild_records"][0]["common_ancestor_policy_key"] == (
            "v2:v2-b-root"
        )
        assert rebuilt["clean_equivalence"]["all_equal"] is True
        decisions = {row["segment_id"]: row for row in rebuilt["segments"]}
        assert decisions[3]["decision"] == "RETAIN"
        assert decisions[3]["reason_code"] == "E_RETENTION_BLOCKS_TARGET"
        status = engine.status()
        assert status["invalidated_policies"][0]["policy_key"] == "v2:v2-b-seg3-old"
        assert status["unresolved_conflicts"] == []
        verified = engine.verify()
        assert verified["status"] == "ok"
        assert verified["view_kind"] == "rebuild"
        assert verified["verified_segments"] == 5
        with sqlite3.connect(config.ledger_db) as conn:
            history = conn.execute(
                "SELECT state, tombstone FROM policy_history "
                "WHERE policy_key = 'v2:v2-b-seg3-old' ORDER BY history_id"
            ).fetchall()
            assert ("loaded", 0) in history
            assert ("invalidated", 1) in history
            invalidation = conn.execute(
                "SELECT * FROM policy_invalidations "
                "WHERE policy_key = 'v2:v2-b-seg3-old'"
            ).fetchone()
            assert invalidation is not None
            assert judged["view_version"] != rebuilt["view_version"]
    finally:
        engine.close()


def test_judge_crash_then_resume_is_idempotent(config: Config) -> None:
    _prepare(config)
    before = json.loads(cli(config, "status").stdout)
    crashed = cli(
        config,
        "judge",
        "--instance",
        "replica-a",
        env={"RETENTION_FAULT_JUDGE_AFTER_STAGED": "1"},
        check=False,
    )
    assert crashed.returncode == 99, crashed.stderr
    with sqlite3.connect(config.ledger_db) as conn:
        assert (
            conn.execute(
                "SELECT COUNT(*) FROM op_calculations WHERE state = 'staged'"
            ).fetchone()[0]
            == 1
        )
    during = json.loads(cli(config, "status").stdout)
    assert during["view_version"] == before["view_version"]
    assert during["view_kind"] == "merge"
    resumed = json.loads(cli(config, "resume").stdout)
    assert resumed["status"] == "recovered"
    assert resumed["recovered_operations"][0]["kind"] == "judge"
    with sqlite3.connect(config.ledger_db) as conn:
        assert (
            conn.execute(
                "SELECT COUNT(*) FROM op_calculations WHERE state = 'committed'"
            ).fetchone()[0]
            == 3
        )
        assert (
            conn.execute(
                "SELECT COUNT(*) FROM retention_windows WHERE instance_id = 'replica-b' "
                "AND logical_clock_ms = 20000"
            ).fetchone()[0]
            == 1
        )
    verified = json.loads(cli(config, "verify").stdout)
    assert verified["status"] == "ok"
    assert verified["verified_segments"] == 5


def test_rebuild_crash_then_resume_has_no_double_carry(config: Config) -> None:
    _prepare(config)
    cli(config, "judge")
    crashed = cli(
        config,
        "rebuild",
        env={"RETENTION_FAULT_REBUILD_AFTER_STAGED": "1"},
        check=False,
    )
    assert crashed.returncode == 99, crashed.stderr
    with sqlite3.connect(config.ledger_db) as conn:
        assert (
            conn.execute(
                "SELECT COUNT(*) FROM rebuild_stages WHERE state = 'staged'"
            ).fetchone()[0]
            == 1
        )
        assert (
            conn.execute("SELECT COUNT(*) FROM policy_invalidations").fetchone()[0] == 0
        )
    before = json.loads(cli(config, "status").stdout)
    resumed = json.loads(cli(config, "resume").stdout)
    assert resumed["status"] == "recovered"
    assert resumed["recovered_operations"][0]["kind"] == "rebuild"
    after = json.loads(cli(config, "status").stdout)
    assert after["view_version"] > before["view_version"]
    assert after["invalidated_policies"][0]["policy_key"] == "v2:v2-b-seg3-old"
    with sqlite3.connect(config.ledger_db) as conn:
        rows = conn.execute(
            "SELECT instance_id, logical_clock_ms, carry_in_units, remainder_units "
            "FROM retention_windows ORDER BY instance_id, logical_clock_ms"
        ).fetchall()
        assert len(rows) == 3
        assert rows == [
            ("replica-a", 20000, 0, 0),
            ("replica-b", 20000, 0, 1000),
            ("replica-c", 20000, 0, 5000),
        ]
    verified = json.loads(cli(config, "verify").stdout)
    assert verified["status"] == "ok"


def test_verify_detects_tampered_decision(config: Config) -> None:
    _prepare(config)
    judged = json.loads(cli(config, "judge").stdout)
    with sqlite3.connect(config.ledger_db) as conn:
        conn.execute(
            "UPDATE segment_decisions SET decision = 'ARCHIVE' "
            "WHERE view_version = ? AND segment_id = 3",
            (judged["view_version"],),
        )
        conn.commit()
    result = cli(config, "verify", check=False)
    assert result.returncode == 2
    error = json.loads(result.stderr)["error"]
    assert error["code"] == "E_VERIFY_MISMATCH"


def test_clock_rollback_error_keeps_committed_state(config: Config) -> None:
    _prepare(config)
    judged = json.loads(cli(config, "judge").stdout)
    result = cli(config, "--clock-ms", "19000", "judge", check=False)
    assert result.returncode == 2
    error = json.loads(result.stderr)["error"]
    assert error["code"] == "E_CLOCK_ROLLBACK"
    status = json.loads(cli(config, "status").stdout)
    assert status["view_version"] == judged["view_version"]
    assert status["content_hash"] == judged["content_hash"]
