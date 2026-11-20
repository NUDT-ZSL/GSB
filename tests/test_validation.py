from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path

import pytest

from dbpool.engine import initialize_database
from dbpool.errors import (
    E_CONFIG_VERSION_ROLLBACK,
    E_DUPLICATE_APPLICATION,
    E_DUPLICATE_ENDPOINT,
    E_INVALID_CONFIG,
    E_INVALID_CLOCK_STEP,
    E_INVALID_LOOPBACK_ADDRESS,
    E_POOL_BOUNDARY,
    E_SAMPLE_DIGEST_MISMATCH,
    PoolError,
)
from dbpool.validation import (
    load_catalog,
    parse_config_object,
    parse_endpoints_object,
    parse_trace_object,
)

from .conftest import SAMPLES, catalog_with, run_cli


def test_duplicate_endpoint_rejected() -> None:
    data = {
        "version": 1,
        "endpoints": [
            {
                "name": "same",
                "address": "tcp://127.0.0.1:19091",
                "max_connections": 1,
                "build_latency_ms": 1,
                "healthy": True,
                "pool_labels": ["read"],
            },
            {
                "name": "same",
                "address": "tcp://127.0.0.1:19092",
                "max_connections": 1,
                "build_latency_ms": 1,
                "healthy": True,
                "pool_labels": ["read"],
            },
        ],
    }
    with pytest.raises(PoolError) as caught:
        parse_endpoints_object(data)
    assert caught.value.code == E_DUPLICATE_ENDPOINT


@pytest.mark.parametrize(
    "address",
    [
        "tcp://192.168.1.2:19091",
        "tcp://10.0.0.1:19091",
        "tcp://example.com:19091",
        "tcp://127.0.0.1:0",
    ],
)
def test_non_loopback_address_rejected(address: str) -> None:
    data = {
        "version": 1,
        "endpoints": [
            {
                "name": "bad",
                "address": address,
                "max_connections": 1,
                "build_latency_ms": 1,
                "healthy": True,
                "pool_labels": ["read"],
            }
        ],
    }
    with pytest.raises(PoolError) as caught:
        parse_endpoints_object(data)
    assert caught.value.code == E_INVALID_LOOPBACK_ADDRESS


def test_duplicate_application_rejected() -> None:
    data = {
        "version": 1,
        "applications": [
            {
                "seq": 1,
                "version": 1,
                "tenant": "t",
                "pool_label": "read",
                "apply_clock_ms": 0,
                "hold_ms": 1,
                "release": True,
            },
            {
                "seq": 1,
                "version": 1,
                "tenant": "t",
                "pool_label": "read",
                "apply_clock_ms": 0,
                "hold_ms": 1,
                "release": True,
            },
        ],
    }
    with pytest.raises(PoolError) as caught:
        parse_trace_object(data)
    assert caught.value.code == E_DUPLICATE_APPLICATION


def test_ipv6_loopback_is_normalized_with_brackets() -> None:
    data = {
        "version": 1,
        "endpoints": [
            {
                "name": "v6",
                "address": "[::1]:19091",
                "max_connections": 1,
                "build_latency_ms": 1,
                "healthy": True,
                "pool_labels": ["read"],
            }
        ],
    }
    endpoints, _ = parse_endpoints_object(data)
    assert endpoints[0].address == "tcp://[::1]:19091"


def test_pool_boundaries_rejected(sample_catalog) -> None:
    base = sample_catalog.config.canonical()
    base["min_idle"] = base["max_total_connections"] + 1
    with pytest.raises(PoolError) as caught:
        parse_config_object(base)
    assert caught.value.code == E_POOL_BOUNDARY

    base = sample_catalog.config.canonical()
    base["clock_step_ms"] = 0
    with pytest.raises(PoolError) as caught:
        parse_config_object(base)
    assert caught.value.code in {E_INVALID_CONFIG, E_INVALID_CLOCK_STEP, E_POOL_BOUNDARY}


def test_invalid_load_creates_no_database(tmp_path: Path) -> None:
    bad_endpoints = tmp_path / "bad-endpoints.json"
    bad_endpoints.write_text(
        json.dumps(
            {
                "version": 1,
                "endpoints": [
                    {
                        "name": "x",
                        "address": "tcp://8.8.8.8:19091",
                        "max_connections": 1,
                        "build_latency_ms": 1,
                        "healthy": True,
                        "pool_labels": ["read"],
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    db = tmp_path / "pool.db"
    result = run_cli(
        "load",
        "--endpoints",
        str(bad_endpoints),
        "--config",
        str(SAMPLES / "pool_config.json"),
        "--trace",
        str(SAMPLES / "trace.json"),
        "--db",
        str(db),
    )
    assert result.returncode == 2
    assert json.loads(result.stderr)["error"]["code"] == E_INVALID_LOOPBACK_ADDRESS
    assert not db.exists()


def test_expected_digest_mismatch_creates_no_database(
    tmp_path: Path, sample_catalog
) -> None:
    db = tmp_path / "pool.db"
    result = run_cli(
        "load",
        "--endpoints",
        str(SAMPLES / "endpoints.json"),
        "--config",
        str(SAMPLES / "pool_config.json"),
        "--trace",
        str(SAMPLES / "trace.json"),
        "--db",
        str(db),
        "--expect-digest",
        "0" * 64,
    )
    assert result.returncode == 2
    assert json.loads(result.stderr)["error"]["code"] == E_SAMPLE_DIGEST_MISMATCH
    assert not db.exists()


def test_config_version_rollback_is_rejected(
    tmp_path: Path, sample_catalog
) -> None:
    db = tmp_path / "pool.db"
    initialize_database(db, sample_catalog)
    version_two = catalog_with(
        sample_catalog,
        config=replace(sample_catalog.config, version=2, per_tenant_quota=3),
    )
    initialize_database(db, version_two)
    with pytest.raises(PoolError) as caught:
        initialize_database(db, sample_catalog)
    assert caught.value.code == E_CONFIG_VERSION_ROLLBACK


def test_sample_digest_mismatch_does_not_mutate_state(
    tmp_path: Path,
    sample_catalog,
) -> None:
    db = tmp_path / "pool.db"
    initialize_database(db, sample_catalog)
    trace = sample_catalog.trace
    changed_apps = trace.applications[:-1] + (
        replace(trace.applications[-1], hold_ms=6),
    )
    changed = catalog_with(
        sample_catalog,
        trace=replace(trace, applications=changed_apps),
    )
    with pytest.raises(PoolError) as caught:
        initialize_database(db, changed)
    assert caught.value.code == E_SAMPLE_DIGEST_MISMATCH


def test_tenant_pool_mapping_is_built(sample_catalog) -> None:
    assert sample_catalog.tenant_pool_labels["tenant-a"] == ("analytics", "read")
    assert sample_catalog.tenant_pool_labels["tenant-d"] == ("write",)
