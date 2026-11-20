from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest

from dbpool.engine import Engine, initialize_database
from dbpool.errors import E_APPLICATION_CONFLICT, Q_CAPACITY, Q_TENANT_QUOTA
from dbpool.model import Application
from dbpool.replay import replay_catalog
from dbpool.transport import SimulatedTransport

from .conftest import catalog_with


def app(
    seq: int,
    *,
    tenant: str = "tenant-a",
    label: str = "read",
    apply: int = 0,
    hold: int = 10,
    release: bool = True,
) -> Application:
    return Application(
        seq=seq,
        version=1,
        tenant=tenant,
        pool_label=label,
        apply_clock_ms=apply,
        hold_ms=hold,
        release=release,
    )


def _catalog(sample_catalog, **config_changes):
    config = replace(sample_catalog.config, **config_changes)
    return catalog_with(sample_catalog, config=config)


def test_tenant_quota_queues_then_reuses_idle(
    tmp_path: Path, sample_catalog
) -> None:
    catalog = _catalog(
        sample_catalog,
        max_total_connections=4,
        per_tenant_quota=1,
        idle_timeout_ms=100,
        min_idle=0,
        tiered_min_idle=3,
    )
    db = tmp_path / "pool.db"
    initialize_database(db, catalog)
    transport = SimulatedTransport()
    with Engine(db, transport=transport) as engine:
        first = engine.acquire(app(1), 0)
        assert first["status"] == "LEASED"
        assert first["lease"]["reused"] is False

        queued = engine.acquire(app(2, apply=1), 1)
        assert queued["status"] == "QUEUED"
        assert queued["reason"] == Q_TENANT_QUOTA

        released = engine.release(1, 1, 2)
        assert released["status"] == "RELEASED"
        assert len(released["dispatched"]) == 1
        dispatched = released["dispatched"][0]
        assert dispatched["app_seq"] == 2
        assert dispatched["reused"] is True
        assert dispatched["conn_id"] == first["lease"]["conn_id"]
        assert dispatched["issued_at_ms"] == 2

        retry = engine.acquire(app(1), 3)
        assert retry["idempotent"] is True
        assert retry["lease"]["lease_id"] == first["lease"]["lease_id"]

        status = engine.status()
        assert status["watermark"]["total_allocated"] == 1
        assert status["watermark"]["total_active"] == 1
        assert status["watermark"]["total_idle"] == 0
        assert status["idle_queue"]["ordered_connection_ids"] == []
        assert len(transport.opened) == 1


def test_total_capacity_queue_and_active_close(
    tmp_path: Path, sample_catalog
) -> None:
    catalog = _catalog(
        sample_catalog,
        max_total_connections=2,
        per_tenant_quota=2,
        idle_timeout_ms=100,
        min_idle=0,
    )
    db = tmp_path / "pool.db"
    initialize_database(db, catalog)
    transport = SimulatedTransport()
    with Engine(db, transport=transport) as engine:
        one = engine.acquire(app(1, tenant="tenant-a"), 0)
        two = engine.acquire(app(2, tenant="tenant-b"), 0)
        queued = engine.acquire(app(3, tenant="tenant-c"), 0)
        assert one["status"] == "LEASED"
        assert two["status"] == "LEASED"
        assert queued["status"] == "QUEUED"
        assert queued["reason"] == Q_CAPACITY

        result = engine.release(1, 1, 1, close=True, reason="EXPLICIT")
        assert result["status"] == "RELEASED"
        assert result["dispatched"][0]["app_seq"] == 3
        assert result["dispatched"][0]["reused"] is False

        repeated = engine.release(1, 1, 20, close=True, reason="EXPLICIT")
        assert repeated["idempotent"] is True
        assert repeated["receipt"]["release_clock_ms"] == 1
        assert repeated["receipt"]["closed"] is True

        status = engine.status()
        assert status["watermark"]["total_allocated"] == 2
        assert status["watermark"]["total_active"] == 2
        assert status["watermark"]["queue_depth"] == 0
        assert status["closed_by_reason"] == [
            {"close_reason": "ACTIVE_CLOSE", "count": 1}
        ]


def test_idle_timeout_preserves_min_idle_and_active_leases(
    tmp_path: Path, sample_catalog
) -> None:
    catalog = _catalog(
        sample_catalog,
        max_total_connections=4,
        per_tenant_quota=2,
        idle_timeout_ms=10,
        min_idle=1,
        clock_step_ms=5,
    )
    db = tmp_path / "pool.db"
    initialize_database(db, catalog)
    with Engine(db, transport=SimulatedTransport()) as engine:
        for seq, tenant in enumerate(("tenant-a", "tenant-b", "tenant-c", "tenant-d"), 1):
            assert engine.acquire(app(seq, tenant=tenant), 0)["status"] == "LEASED"
        for seq in (1, 2, 3):
            engine.release(seq, 1, 1)
        result = engine.advance(100)
        assert len(result["closed"]) == 2
        status = engine.status()
        assert status["watermark"]["total_allocated"] == 2
        assert status["watermark"]["total_active"] == 1
        assert status["watermark"]["total_idle"] == 1
        assert status["closed_by_reason"] == [
            {"close_reason": "IDLE_TIMEOUT", "count": 2}
        ]
        active = status["active_leases"]
        assert len(active) == 1
        assert active[0]["app_seq"] == 4


def test_quota_shrink_closes_only_idle_connections(
    tmp_path: Path, sample_catalog
) -> None:
    v1 = _catalog(
        sample_catalog,
        version=1,
        max_total_connections=4,
        per_tenant_quota=2,
        idle_timeout_ms=100,
        min_idle=0,
    )
    db = tmp_path / "pool.db"
    initialize_database(db, v1)
    with Engine(db, transport=SimulatedTransport()) as engine:
        engine.acquire(app(1), 0)
        engine.acquire(app(2, hold=20), 0)
        engine.release(2, 1, 1)
        assert engine.status()["watermark"]["total_idle"] == 1

    v2 = _catalog(
        sample_catalog,
        version=2,
        max_total_connections=4,
        per_tenant_quota=1,
        idle_timeout_ms=100,
        min_idle=0,
    )
    initialize_database(db, v2)
    with Engine(db, transport=SimulatedTransport()) as engine:
        status = engine.status()
        assert status["config_version"] == 2
        assert status["watermark"]["total_allocated"] == 1
        assert status["watermark"]["total_active"] == 1
        assert status["watermark"]["total_idle"] == 0
        assert status["closed_by_reason"] == [
            {"close_reason": "QUOTA_SHRINK", "count": 1}
        ]


def test_deferred_quota_shrink_after_active_release(
    tmp_path: Path, sample_catalog
) -> None:
    v1 = _catalog(
        sample_catalog,
        version=1,
        max_total_connections=4,
        per_tenant_quota=2,
        idle_timeout_ms=100,
        min_idle=0,
    )
    db = tmp_path / "pool.db"
    initialize_database(db, v1)
    with Engine(db, transport=SimulatedTransport()) as engine:
        engine.acquire(app(1), 0)
        engine.acquire(app(2), 0)

    v2 = _catalog(
        sample_catalog,
        version=2,
        max_total_connections=4,
        per_tenant_quota=1,
        idle_timeout_ms=100,
        min_idle=0,
    )
    initialize_database(db, v2)
    with Engine(db, transport=SimulatedTransport()) as engine:
        before = engine.status()
        assert before["watermark"]["total_active"] == 2
        assert before["watermark"]["total_idle"] == 0
        released = engine.release(2, 1, 1)
        assert any(
            item["reason"] == "QUOTA_SHRINK" for item in released["closed"]
        )
        after = engine.status()
        assert after["watermark"]["total_allocated"] == 1
        assert after["watermark"]["total_active"] == 1
        assert after["watermark"]["total_idle"] == 0


def test_idle_connection_is_not_reused_across_tenants(
    tmp_path: Path, sample_catalog
) -> None:
    catalog = _catalog(
        sample_catalog,
        max_total_connections=2,
        per_tenant_quota=1,
        idle_timeout_ms=100,
        min_idle=0,
    )
    db = tmp_path / "pool.db"
    initialize_database(db, catalog)
    with Engine(db, transport=SimulatedTransport()) as engine:
        first = engine.acquire(app(1, tenant="tenant-a"), 0)
        engine.release(1, 1, 1)
        second = engine.acquire(app(2, tenant="tenant-b"), 2)
        assert second["status"] == "LEASED"
        assert second["lease"]["conn_id"] != first["lease"]["conn_id"]
        status = engine.status()
        assert status["watermark"]["total_allocated"] == 2
        assert status["watermark"]["total_idle"] == 1


def test_same_application_with_different_content_conflicts(
    tmp_path: Path, sample_catalog
) -> None:
    db = tmp_path / "pool.db"
    initialize_database(db, sample_catalog)
    with Engine(db, transport=SimulatedTransport()) as engine:
        first = engine.acquire(app(1), 0)
        assert first["status"] == "LEASED"
        with pytest.raises(Exception) as caught:
            engine.acquire(app(1, hold=99), 1)
        assert getattr(caught.value, "code") == E_APPLICATION_CONFLICT
        status = engine.status()
        assert status["watermark"]["total_allocated"] == 1


def test_sample_replay_switches_modes_and_is_deterministic(
    tmp_path: Path, sample_catalog
) -> None:
    first = replay_catalog(
        tmp_path / "one.db",
        sample_catalog,
        transport_factory=SimulatedTransport,
    )
    second = replay_catalog(
        tmp_path / "two.db",
        sample_catalog,
        transport_factory=SimulatedTransport,
    )
    assert first["leases_issued"] == second["leases_issued"]
    assert first["status"] == second["status"]
    switches = first["status"]["algorithm"]["switches"]
    assert any(
        item["from_mode"] == "exact" and item["to_mode"] == "tiered"
        for item in switches
    )
    assert first["leases_issued"][8]["reused"] is True
    assert first["leases_issued"][9]["reused"] is True
    assert first["status"]["watermark"]["total_allocated"] == 2
    assert first["status"]["watermark"]["total_idle"] == 2
