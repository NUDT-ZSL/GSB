from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from .conftest import cli_json, run_cli


def _load(state_path: Path) -> None:
    cli_json(
        "load",
        "--sample",
        "samples/sample.json",
        "--clock",
        "1000",
        state_path=state_path,
    )


def _query_rows(state_path: Path) -> list[dict]:
    connection = sqlite3.connect(state_path)
    connection.row_factory = sqlite3.Row
    try:
        return [
            dict(row)
            for row in connection.execute(
                "SELECT query_id, status, rule_version FROM queries ORDER BY query_id"
            )
        ]
    finally:
        connection.close()


def test_crash_after_execute_recovers_from_state(tmp_path: Path):
    state_path = tmp_path / "state.db"
    _load(state_path)
    crashed = run_cli(
        "query",
        "--template",
        "orders_all",
        "--clock",
        "1100",
        "--crash-at",
        "after_execute",
        state_path=state_path,
        check=False,
    )
    assert crashed.returncode == 75
    rows = _query_rows(state_path)
    assert len(rows) == 1
    assert rows[0]["status"] == "EXECUTED"
    connection = sqlite3.connect(state_path)
    try:
        assert connection.execute("SELECT COUNT(*) FROM shard_outcomes").fetchone()[0] == 4
        assert connection.execute("SELECT COUNT(*) FROM query_results").fetchone()[0] == 0
    finally:
        connection.close()
    resumed = cli_json("resume", "--clock", "1200", state_path=state_path)
    assert resumed["resumed_count"] == 1
    assert resumed["cleaned"][0]["removed_shard_outcomes"] == 4
    assert resumed["completed"][0]["overall_status"] == "SUCCESS"
    assert resumed["completed"][0]["query_id"] == rows[0]["query_id"]
    assert verify_ok(state_path)
    second = cli_json("resume", "--clock", "1300", state_path=state_path)
    assert second["resumed_count"] == 0
    connection = sqlite3.connect(state_path)
    try:
        assert connection.execute("SELECT COUNT(*) FROM query_results").fetchone()[0] == 1
    finally:
        connection.close()


def test_crash_after_merge_cleans_staging_without_duplicate_commit(tmp_path: Path):
    state_path = tmp_path / "state.db"
    _load(state_path)
    crashed = run_cli(
        "query",
        "--template",
        "orders_sum_count",
        "--clock",
        "1100",
        "--crash-at",
        "after_merge",
        state_path=state_path,
        check=False,
    )
    assert crashed.returncode == 75
    connection = sqlite3.connect(state_path)
    try:
        assert connection.execute("SELECT status FROM queries").fetchone()[0] == "MERGED"
        assert connection.execute("SELECT COUNT(*) FROM query_staging").fetchone()[0] == 1
        assert connection.execute("SELECT COUNT(*) FROM query_results").fetchone()[0] == 0
    finally:
        connection.close()
    resumed = cli_json("resume", "--clock", "1200", state_path=state_path)
    assert resumed["cleaned"][0]["removed_staging"] == 1
    assert resumed["completed"][0]["aggregate"] == {"row_count": 12, "total_amount": 780}
    assert verify_ok(state_path)


def test_resume_keeps_original_rule_binding_after_apply(tmp_path: Path):
    state_path = tmp_path / "state.db"
    _load(state_path)
    crashed = run_cli(
        "query",
        "--template",
        "orders_eq",
        "--clock",
        "1100",
        "--crash-at",
        "after_rewrite",
        state_path=state_path,
        check=False,
    )
    assert crashed.returncode == 75
    row = _query_rows(state_path)[0]
    assert row["rule_version"] == "r1"
    applied = cli_json(
        "apply",
        "--sample",
        "samples/sample.json",
        "--version",
        "r2",
        "--clock",
        "2000",
        state_path=state_path,
    )
    assert applied["generation"] == 2
    resumed = cli_json("resume", "--clock", "2100", state_path=state_path)
    result = resumed["completed"][0]
    assert result["rule_version"] == "r1"
    assert result["target_shards"] == [2]
    assert result["rows"][0]["customer_id"] == 6
    assert verify_ok(state_path)


def test_recovery_uses_only_persistent_query_and_rule_state(tmp_path: Path):
    state_path = tmp_path / "state.db"
    _load(state_path)
    crashed = run_cli(
        "query",
        "--template",
        "orders_in",
        "--clock",
        "1100",
        "--crash-at",
        "after_route",
        state_path=state_path,
        check=False,
    )
    assert crashed.returncode == 75
    connection = sqlite3.connect(state_path)
    try:
        request = json.loads(
            connection.execute("SELECT request_json FROM queries").fetchone()[0]
        )
    finally:
        connection.close()
    assert request["template"]["id"] == "orders_in"
    assert request["rule_version"] == "r1"
    assert request["query_key"].startswith("qk_")
    resumed = cli_json("resume", "--clock", "1200", state_path=state_path)
    assert resumed["completed"][0]["overall_status"] == "SUCCESS"
    assert verify_ok(state_path)


def verify_ok(state_path: Path) -> bool:
    return cli_json("verify", state_path=state_path)["ok"]

