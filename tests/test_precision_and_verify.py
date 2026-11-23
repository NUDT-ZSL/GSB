from __future__ import annotations

from decimal import Decimal

from conftest import DEFAULT_PARTITIONS


def test_precision_sensitive_records_include_error_source_chain(workspace) -> None:
    streams = [
        {
            "tenant": "tenant-b",
            "metric": "metric.b",
            "tags": {"host": "b"},
            "sample_period_ms": 500,
            "sample_bytes": 10,
        },
        {
            "tenant": "tenant-c",
            "metric": "metric.c",
            "tags": {"host": "c"},
            "sample_period_ms": 500,
            "sample_bytes": 10,
        },
    ]
    quotas = {
        "window_ms": 5000,
        "fixed_scale": 1000,
        "comparison_tolerance_bytes": "0.2",
        "tenants": {
            "tenant-b": {
                "window_limit_bytes": "9.95",
                "borrow_limit_bytes": "0",
                "allow_borrow": False,
            },
            "tenant-c": {
                "window_limit_bytes": "10.05",
                "borrow_limit_bytes": "0",
                "allow_borrow": False,
            },
        },
    }
    engine, _paths = workspace.create(streams=streams, quotas=quotas)
    engine.ingest_document(
        {
            "events": [
                {
                    "tenant": "tenant-b",
                    "metric": "metric.b",
                    "logical_time_ms": 1000,
                    "sample_seq": 1,
                    "event_id": "pb",
                },
                {
                    "tenant": "tenant-c",
                    "metric": "metric.c",
                    "logical_time_ms": 1000,
                    "sample_seq": 1,
                    "event_id": "pc",
                },
            ]
        }
    )
    engine.flush(clock_ms=1000)
    status = engine.status()
    assert status["precision_sensitive"]
    record = status["precision_sensitive"][0]
    assert record["kind"] == "TENANT"
    assert record["decision_depends"] is True
    assert record["delta_units"] < record["tolerance_units"]
    for chain in record["sources"].values():
        assert chain
        assert all("register_id" in item for item in chain)
        assert all("step" in item for item in chain)
        assert all("window_id" in item for item in chain)
    assert engine.verify()["ok"] is True


def test_declared_conversion_records_discarded_remainder(workspace) -> None:
    quotas = {
        "window_ms": 5000,
        "fixed_scale": 1000,
        "comparison_tolerance_bytes": "0.0005",
        "tenants": {
            "tenant-a": {
                "window_limit_bytes": "1.2345",
                "borrow_limit_bytes": "0.0005",
                "allow_borrow": False,
            }
        },
    }
    engine, _paths = workspace.create(quotas=quotas)
    conn = engine.store._connect()
    try:
        row = conn.execute(
            "SELECT * FROM precision_ops WHERE step='window_limit_scale' "
            "AND tenant='tenant-a'"
        ).fetchone()
    finally:
        conn.close()
    assert row["scale"] == 1000
    assert row["rounding_direction"] == "FLOOR"
    assert Decimal(row["discarded_remainder_units"]) == Decimal("0.5")
    assert Decimal(row["cumulative_error_units"]) == Decimal("0.5")


def test_status_is_stable_and_contains_required_sections(workspace) -> None:
    engine, _paths = workspace.create()
    engine.ingest_document(
        {
            "events": [
                {
                    "tenant": "tenant-a",
                    "metric": "metric.one",
                    "logical_time_ms": 1000,
                    "sample_seq": 1,
                    "event_id": "status-one",
                }
            ]
        }
    )
    first = engine.status()
    second = engine.status()
    assert first == second
    for key in (
        "version",
        "logical_clock_ms",
        "token",
        "buffer_depth",
        "partitions",
        "tenants",
        "precision_sensitive",
    ):
        assert key in first
    assert engine.verify()["manifest_digest"]

