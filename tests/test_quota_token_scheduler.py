from __future__ import annotations

import pytest

from metric_engine.errors import EngineError

from conftest import DEFAULT_PARTITIONS, DEFAULT_QUOTAS, DEFAULT_STREAMS, DEFAULT_TOKENS, event


def test_duplicate_sample_is_idempotent_and_window_freezes(workspace) -> None:
    engine, _paths = workspace.create()
    doc = {"events": [event(event_id="same")]}
    first = engine.ingest_document(doc)
    second = engine.ingest_document(doc)
    assert first["decisions"][0]["decision"] == "BUFFERED"
    assert second["decisions"][0]["decision"] == "IDEMPOTENT"
    engine.flush(clock_ms=2500)
    status = engine.status()
    window = status["tenants"][0]["windows"][0]
    assert window["used_bytes"] == "4"
    assert window["status"] == "ACTIVE"

    engine.ingest_document({"events": [event(logical_time_ms=6000, sample_seq=2, event_id="next")]})
    engine.flush(clock_ms=6500)
    status = engine.status()
    windows = status["tenants"][0]["windows"]
    assert windows[0]["status"] == "FROZEN"
    assert windows[0]["frozen_remaining_units"] == 96000
    assert windows[1]["used_units"] == 4000

    third = engine.ingest_document(doc)
    assert third["decisions"][0]["decision"] == "IDEMPOTENT"
    engine.flush(clock_ms=6500)
    conn = engine.store._connect()
    try:
        charges = conn.execute(
            "SELECT COUNT(*) AS n FROM quota_charges WHERE sample_id='same'"
        ).fetchone()["n"]
        tokens = conn.execute(
            "SELECT COUNT(*) AS n FROM token_charges WHERE sample_id='same'"
        ).fetchone()["n"]
    finally:
        conn.close()
    assert charges == 1
    assert tokens == 1


def test_joint_constraint_returns_buffer_and_records_first_constraint(workspace) -> None:
    streams = [
        {
            "tenant": "tenant-a",
            "metric": "metric.one",
            "tags": {"host": "h1"},
            "sample_period_ms": 500,
            "sample_bytes": 10,
        }
    ]
    quotas = {
        "window_ms": 5000,
        "fixed_scale": 1000,
        "comparison_tolerance_bytes": "0",
        "tenants": {
            "tenant-a": {
                "window_limit_bytes": "100",
                "borrow_limit_bytes": "0",
                "allow_borrow": False,
            }
        },
    }
    tokens = {**DEFAULT_TOKENS, "capacity_tokens": 1, "refill_rate_per_second": 1}
    engine, _paths = workspace.create(streams=streams, quotas=quotas, tokens=tokens)
    engine.ingest_document(
        {
            "events": [
                event(sample_seq=1, event_id="one"),
                event(sample_seq=2, event_id="two"),
            ]
        }
    )
    result = engine.flush(clock_ms=1000)
    round_result = result["rounds"][0]
    assert round_result["persisted_count"] == 1
    buffered = [item for item in round_result["attempts"] if item["outcome"] == "BUFFERED"]
    assert buffered
    assert buffered[0]["first_constraint"]["constraint"] == "global_token"
    status = engine.status()
    assert status["buffer_depth"] == 1
    assert status["token"]["balance_units"] == 0


def test_quota_is_checked_before_token_and_buffers_without_drop(workspace) -> None:
    streams = [
        {
            "tenant": "tenant-a",
            "metric": "metric.one",
            "tags": {"host": "h1"},
            "sample_period_ms": 500,
            "sample_bytes": 10,
        }
    ]
    quotas = {
        "window_ms": 5000,
        "fixed_scale": 1000,
        "comparison_tolerance_bytes": "0",
        "tenants": {
            "tenant-a": {
                "window_limit_bytes": "5",
                "borrow_limit_bytes": "0",
                "allow_borrow": False,
            }
        },
    }
    tokens = {**DEFAULT_TOKENS, "capacity_tokens": 0}
    # Capacity must be positive, so keep one token but force quota to fail first.
    tokens["capacity_tokens"] = 1
    engine, _paths = workspace.create(streams=streams, quotas=quotas, tokens=tokens)
    engine.ingest_document({"events": [event(event_id="q")]})
    result = engine.flush(clock_ms=1000)
    attempt = result["rounds"][0]["attempts"][0]
    assert attempt["outcome"] == "BUFFERED"
    assert attempt["first_constraint"]["constraint"] == "tenant_quota"
    assert engine.status()["buffer_depth"] == 1


def test_borrow_is_booked_and_return_obligation_recorded(workspace) -> None:
    streams = [
        {
            "tenant": "tenant-a",
            "metric": "metric.one",
            "tags": {"host": "h1"},
            "sample_period_ms": 500,
            "sample_bytes": 10,
        }
    ]
    quotas = {
        "window_ms": 5000,
        "fixed_scale": 1000,
        "comparison_tolerance_bytes": "0",
        "tenants": {
            "tenant-a": {
                "window_limit_bytes": "8",
                "borrow_limit_bytes": "4",
                "allow_borrow": True,
            }
        },
    }
    engine, _paths = workspace.create(streams=streams, quotas=quotas)
    engine.ingest_document({"events": [event(event_id="borrow")]})
    engine.flush(clock_ms=1000)
    window = engine.status()["tenants"][0]["windows"][0]
    assert window["used_units"] == 10000
    assert window["borrowed_units"] == 2000
    assert window["return_obligation_units"] == 2000
    conn = engine.store._connect()
    try:
        row = conn.execute(
            "SELECT * FROM borrow_ledger WHERE sample_id='borrow'"
        ).fetchone()
    finally:
        conn.close()
    assert row["borrowed_units"] == 2000
    assert row["repay_required_units"] == 2000
    assert row["status"] == "OUTSTANDING"


def test_round_limit_defers_and_budget_abandons_with_distinct_outcomes(workspace) -> None:
    streams = [
        {
            "tenant": "tenant-a",
            "metric": "metric.one",
            "tags": {"host": "h1"},
            "sample_period_ms": 600,
            "sample_bytes": 4,
        }
    ]
    partitions = {
        **DEFAULT_PARTITIONS,
        "flush_round_limit": 1,
        "round_time_budget_ms": 1000,
        "partition_count": 1,
    }
    engine, _paths = workspace.create(streams=streams, partitions=partitions)
    engine.ingest_document(
        {
            "events": [
                event(sample_seq=1, event_id="s1"),
                event(sample_seq=2, event_id="s2"),
            ]
        }
    )
    first = engine.flush(clock_ms=1000)
    assert first["rounds"][0]["persisted_count"] == 1
    assert first["rounds"][0]["deferred_count"] == 1
    assert engine.status()["buffer_depth"] == 1

    engine.flush(clock_ms=1000)
    assert engine.status()["buffer_depth"] == 0
    assert engine.verify()["persisted_samples"] == 2


def test_budget_abandon_marks_not_started_then_dropped(workspace) -> None:
    streams = [
        {
            "tenant": "tenant-a",
            "metric": "metric.one",
            "tags": {"host": "h1"},
            "sample_period_ms": 600,
            "sample_bytes": 4,
        }
    ]
    partitions = {
        **DEFAULT_PARTITIONS,
        "round_time_budget_ms": 500,
        "partition_count": 1,
    }
    engine, _paths = workspace.create(streams=streams, partitions=partitions)
    engine.ingest_document({"events": [event(event_id="budget")]})
    result = engine.flush(clock_ms=1000)
    attempt = result["rounds"][0]["attempts"][0]
    assert attempt["start_state"] == "NOT_STARTED"
    assert attempt["outcome"] == "NOT_STARTED_BUDGET"
    assert attempt["terminal_state"] == "ABANDONED"
    status = engine.status()
    assert status["buffer_depth"] == 0
    conn = engine.store._connect()
    try:
        sample = conn.execute(
            "SELECT state, decision, reason FROM samples WHERE sample_id='budget'"
        ).fetchone()
    finally:
        conn.close()
    assert sample["state"] == "DROPPED"
    assert sample["decision"] == "ABANDONED_BUDGET"
    assert sample["reason"] == "ROUND_BUDGET_EXHAUSTED"


def test_buffer_full_flushes_then_drops_only_if_constraint_still_blocks(workspace) -> None:
    streams = [
        {
            "tenant": "tenant-a",
            "metric": "metric.one",
            "tags": {"host": "h1"},
            "sample_period_ms": 500,
            "sample_bytes": 10,
        }
    ]
    partitions = {**DEFAULT_PARTITIONS, "buffer_per_partition": 1, "partition_count": 1}
    quotas = {
        "window_ms": 5000,
        "fixed_scale": 1000,
        "comparison_tolerance_bytes": "0",
        "tenants": {
            "tenant-a": {
                "window_limit_bytes": "5",
                "borrow_limit_bytes": "0",
                "allow_borrow": False,
            }
        },
    }
    engine, _paths = workspace.create(
        streams=streams, partitions=partitions, quotas=quotas
    )
    report = engine.ingest_document(
        {
            "events": [
                event(sample_seq=1, event_id="b1"),
                event(sample_seq=2, event_id="b2"),
            ]
        }
    )
    assert report["decisions"][1]["decision"] == "DROPPED"
    assert report["decisions"][1]["reason"] == "BUFFER_FULL_AFTER_FLUSH"
    assert engine.status()["buffer_depth"] == 1
    conn = engine.store._connect()
    try:
        row = conn.execute(
            "SELECT state, decision FROM samples WHERE sample_id='b2'"
        ).fetchone()
    finally:
        conn.close()
    assert row["state"] == "DROPPED"
    assert row["decision"] == "DROPPED_BUFFER_FULL"

