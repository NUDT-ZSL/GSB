from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest

from dbpool.engine import Engine, initialize_database
from dbpool.errors import (
    E_BUILD_BUDGET_EXCEEDED,
    E_ENDPOINT_REFUSED,
    E_MALFORMED_PROBE,
)
from dbpool.model import Application, Endpoint
from dbpool.transport import LoopbackTransport, SimulatedTransport

from .conftest import catalog_with, free_port


def app(
    seq: int,
    *,
    tenant: str,
    label: str = "read",
    hold: int = 10,
) -> Application:
    return Application(
        seq=seq,
        version=1,
        tenant=tenant,
        pool_label=label,
        apply_clock_ms=0,
        hold_ms=hold,
        release=True,
    )


def test_build_budget_exceeded_is_atomic(
    tmp_path: Path, sample_catalog
) -> None:
    config = replace(sample_catalog.config, round_budget_ms=1)
    catalog = catalog_with(sample_catalog, config=config)
    db = tmp_path / "pool.db"
    initialize_database(db, catalog)
    with Engine(db, transport=SimulatedTransport()) as engine:
        with pytest.raises(Exception) as caught:
            engine.acquire(app(1, tenant="tenant-a"), 0)
        assert getattr(caught.value, "code") == E_BUILD_BUDGET_EXCEEDED
        status = engine.status()
        assert status["watermark"]["total_allocated"] == 0
        assert status["active_leases"] == []
        assert status["queue"] == []


def test_simulated_malformed_probe_does_not_pollute_other_tenants(
    tmp_path: Path, sample_catalog
) -> None:
    db = tmp_path / "pool.db"
    initialize_database(db, sample_catalog)
    transport = SimulatedTransport(malformed={"echo-b"})
    with Engine(db, transport=transport) as engine:
        healthy = engine.acquire(app(1, tenant="tenant-b", label="read"), 0)
        assert healthy["status"] == "LEASED"
        assert healthy["lease"]["endpoint_name"] == "echo-a"
        with pytest.raises(Exception) as caught:
            engine.acquire(app(2, tenant="tenant-a", label="analytics"), 1)
        assert getattr(caught.value, "code") == E_MALFORMED_PROBE
        status = engine.status()
        assert status["watermark"]["total_allocated"] == 1
        assert status["watermark"]["total_active"] == 1
        assert len(status["active_leases"]) == 1
        assert status["active_leases"][0]["app_seq"] == 1
        assert status["pending_intents"] == []


def test_real_loopback_refusal_maps_to_business_error(
    tmp_path: Path, sample_catalog
) -> None:
    port = free_port()
    endpoint = Endpoint(
        name="refused",
        address=f"tcp://127.0.0.1:{port}",
        host="127.0.0.1",
        port=port,
        max_connections=2,
        build_latency_ms=1,
        healthy=True,
        pool_labels=("read", "write", "analytics"),
    )
    catalog = catalog_with(sample_catalog, endpoints=(endpoint,))
    db = tmp_path / "pool.db"
    initialize_database(db, catalog)
    with Engine(db, transport=LoopbackTransport()) as engine:
        with pytest.raises(Exception) as caught:
            engine.acquire(app(1, tenant="tenant-a", label="read"), 0)
        assert getattr(caught.value, "code") == E_ENDPOINT_REFUSED
        status = engine.status()
        assert status["watermark"]["total_allocated"] == 0
        assert status["pending_intents"] == []


def test_real_loopback_malformed_probe_maps_to_business_error(
    tmp_path: Path,
    sample_catalog,
    malformed_service: int,
) -> None:
    endpoint = Endpoint(
        name="malformed",
        address=f"tcp://127.0.0.1:{malformed_service}",
        host="127.0.0.1",
        port=malformed_service,
        max_connections=2,
        build_latency_ms=1,
        healthy=True,
        pool_labels=("read", "write", "analytics"),
    )
    catalog = catalog_with(sample_catalog, endpoints=(endpoint,))
    db = tmp_path / "pool.db"
    initialize_database(db, catalog)
    with Engine(db, transport=LoopbackTransport()) as engine:
        with pytest.raises(Exception) as caught:
            engine.acquire(app(1, tenant="tenant-a", label="read"), 0)
        assert getattr(caught.value, "code") == E_MALFORMED_PROBE
        status = engine.status()
        assert status["watermark"]["total_allocated"] == 0


def test_real_loopback_echo_leases_connection(
    tmp_path: Path,
    live_catalog,
) -> None:
    db = tmp_path / "pool.db"
    initialize_database(db, live_catalog)
    with Engine(db, transport=LoopbackTransport()) as engine:
        result = engine.acquire(app(1, tenant="tenant-a", label="read"), 0)
        assert result["status"] == "LEASED"
        status = engine.status()
        assert status["watermark"]["total_allocated"] == 1
        assert status["active_leases"][0]["lease_id"] == result["lease"]["lease_id"]
