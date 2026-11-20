from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from pathlib import Path

from dbpool.engine import initialize_database
from dbpool.model import Application, Trace

from .conftest import catalog_with, cli_json, run_cli


def _trace(count: int) -> Trace:
    applications = tuple(
        Application(
            seq=index,
            version=1,
            tenant=f"tenant-{index:02d}",
            pool_label="read",
            apply_clock_ms=0,
            hold_ms=100,
            release=False,
        )
        for index in range(1, count + 1)
    )
    return Trace(version=1, applications=applications, final_clock_ms=0)


def _catalog(live_catalog, count: int = 12):
    config = replace(
        live_catalog.config,
        max_total_connections=4,
        per_tenant_quota=4,
        idle_timeout_ms=100,
        min_idle=0,
    )
    return catalog_with(
        live_catalog,
        config=config,
        trace=_trace(count),
    )


def _acquire(db: Path, seq: int, clock: int = 0):
    return run_cli(
        "acquire",
        "--db",
        str(db),
        "--seq",
        str(seq),
        "--clock-ms",
        str(clock),
    )


def test_parallel_acquires_do_not_overbuild(
    tmp_path: Path,
    live_catalog,
) -> None:
    catalog = _catalog(live_catalog)
    db = tmp_path / "pool.db"
    initialize_database(db, catalog)

    with ThreadPoolExecutor(max_workers=12) as pool:
        results = list(pool.map(lambda seq: _acquire(db, seq), range(1, 13)))
    assert all(result.returncode == 0 for result in results), [
        result.stderr for result in results if result.returncode != 0
    ]
    payloads = [json.loads(result.stdout) for result in results]
    assert sum(item["status"] == "LEASED" for item in payloads) == 4
    assert sum(item["status"] == "QUEUED" for item in payloads) == 8

    status = cli_json("status", "--db", str(db))
    assert status["watermark"]["total_allocated"] == 4
    assert status["watermark"]["total_active"] == 4
    assert status["watermark"]["queue_depth"] == 8
    assert len(status["active_leases"]) == 4


def test_same_application_is_issued_once_across_processes(
    tmp_path: Path,
    live_catalog,
) -> None:
    catalog = _catalog(live_catalog, count=1)
    db = tmp_path / "pool.db"
    initialize_database(db, catalog)

    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(lambda _: _acquire(db, 1), range(8)))
    assert all(result.returncode == 0 for result in results)
    payloads = [json.loads(result.stdout) for result in results]
    lease_ids = {item["lease"]["lease_id"] for item in payloads}
    assert len(lease_ids) == 1
    assert sum(not item["idempotent"] for item in payloads) == 1

    status = cli_json("status", "--db", str(db))
    assert status["watermark"]["total_allocated"] == 1
    assert len(status["active_leases"]) == 1


def test_parallel_releases_are_idempotent(
    tmp_path: Path,
    live_catalog,
) -> None:
    catalog = _catalog(live_catalog, count=1)
    db = tmp_path / "pool.db"
    initialize_database(db, catalog)
    acquired = _acquire(db, 1)
    assert acquired.returncode == 0, acquired.stderr

    def release(_: int):
        return run_cli(
            "release",
            "--db",
            str(db),
            "--seq",
            "1",
            "--clock-ms",
            "10",
        )

    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(release, range(8)))
    assert all(result.returncode == 0 for result in results)
    payloads = [json.loads(result.stdout) for result in results]
    assert sum(not item["idempotent"] for item in payloads) == 1
    assert {item["receipt"]["release_clock_ms"] for item in payloads} == {10}

    status = cli_json("status", "--db", str(db))
    assert status["watermark"]["total_allocated"] == 1
    assert status["watermark"]["total_active"] == 0
    assert status["watermark"]["total_idle"] == 1
    assert len(status["released_leases"]) == 1
