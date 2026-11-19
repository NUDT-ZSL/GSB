from __future__ import annotations

import json

from shardroute.engine import QueryEngine
from shardroute.injections import InjectionSet
from shardroute.verify import verify_state

from .conftest import INJECTIONS_PATH


def test_cross_shard_sum_and_count(state):
    result = QueryEngine(state, 1100).query("orders_sum_count")
    assert result["overall_status"] == "SUCCESS"
    assert result["target_shards"] == [0, 1, 2, 3]
    assert result["aggregate"] == {"row_count": 12, "total_amount": 780}
    assert [item["shard_id"] for item in result["shard_outcomes"]] == [0, 1, 2, 3]
    assert verify_state(state)["ok"] is True


def test_routed_aggregate_uses_only_target_shards(state):
    result = QueryEngine(state, 1100).query("orders_agg_range")
    assert result["target_shards"] == [2, 3]
    assert result["aggregate"] == {"row_count": 2, "total_amount": 70}


def test_select_merge_is_globally_sorted(state):
    result = QueryEngine(state, 1100).query("orders_all")
    customer_ids = [row["customer_id"] for row in result["rows"]]
    assert customer_ids == list(range(12))
    assert sum(row["amount"] for row in result["rows"]) == 780


def test_in_query_returns_deterministic_rows(state):
    result = QueryEngine(state, 1100).query("orders_in")
    assert [row["customer_id"] for row in result["rows"]] == [1, 6, 10]
    assert [row["amount"] for row in result["rows"]] == [20, 70, 110]


def test_repeated_same_query_is_idempotent(state):
    first = QueryEngine(state, 1100).query("orders_eq")
    second = QueryEngine(state, 1100).query("orders_eq")
    assert first == second
    connection = state.connect()
    try:
        result_count = connection.execute("SELECT COUNT(*) FROM query_results").fetchone()[0]
        query_count = connection.execute("SELECT COUNT(*) FROM queries").fetchone()[0]
    finally:
        connection.close()
    assert result_count == 1
    assert query_count == 1


def test_partial_shard_failure_is_distinguished(state):
    result = QueryEngine(
        state,
        1100,
        injections=InjectionSet.load(INJECTIONS_PATH),
    ).query("orders_all")
    assert result["overall_status"] == "PARTIAL"
    assert result["error_code"] == "E_PARTIAL_SHARD_FAILURE"
    assert result["error_codes"] == ["E_PHYSICAL_TABLE_MISSING"]
    failed = [item for item in result["shard_outcomes"] if item["status"] == "FAILED"]
    assert len(failed) == 1
    assert failed[0]["shard_id"] == 2
    assert failed[0]["error_code"] == "E_PHYSICAL_TABLE_MISSING"
    assert result["aggregate"] is None
    assert len(result["rows"]) == 9
    assert verify_state(state)["ok"] is True


def test_result_digest_is_stable_in_persisted_json(state):
    result = QueryEngine(state, 1100).query("orders_eq")
    connection = state.connect()
    try:
        row = connection.execute(
            "SELECT result_json, result_digest FROM query_results"
        ).fetchone()
    finally:
        connection.close()
    assert json.loads(row["result_json"]) == result
    assert row["result_digest"] == result["result_digest"]

