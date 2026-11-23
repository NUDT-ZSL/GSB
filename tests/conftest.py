from __future__ import annotations

import json
from pathlib import Path
import sqlite3
import subprocess
import sys
from typing import Any

import pytest

from quota_ledger.engine import LedgerEngine


ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def db_path(tmp_path: Path) -> Path:
    return tmp_path / "ledger.db"


@pytest.fixture
def engine(db_path: Path) -> LedgerEngine:
    instance = LedgerEngine(db_path)
    instance.initialize()
    return instance


def tenant_doc(
    *,
    quota_mc: int = 100,
    quota_mib: int = 100,
    allow_overdraft: bool = False,
    borrow_mc: int = 0,
    borrow_mib: int = 0,
    version: int = 1,
    pool_capacity_mc: int = 500,
    pool_capacity_mib: int = 500,
    name: str = "tenant-a",
) -> dict[str, Any]:
    return {
        "fixed_scale": 1000,
        "precision_tolerance": 0.000001,
        "resource_pools": [
            {
                "name": "pool-main",
                "millicores": pool_capacity_mc,
                "mib": pool_capacity_mib,
            }
        ],
        "tenants": [
            {
                "name": name,
                "resource_pool": "pool-main",
                "quota": {"millicores": quota_mc, "mib": quota_mib},
                "allow_overdraft": allow_overdraft,
                "borrow_limit": {
                    "millicores": borrow_mc,
                    "mib": borrow_mib,
                },
                "declaration_version": version,
            }
        ],
    }


def service_doc(
    *,
    batch: int = 20,
    concurrency: int = 20,
    name: str = "svc-a",
) -> dict[str, Any]:
    return {
        "services": [
            {
                "name": name,
                "resource_pool": "pool-main",
                "requests_per_round": batch,
                "max_concurrent_reservations": concurrency,
            }
        ]
    }


def app(
    sequence: int,
    *,
    application_id: str | None = None,
    tenant: str = "tenant-a",
    service: str = "svc-a",
    mc: int = 10,
    mib: int = 10,
    at: int = 0,
    duration: int = 1000,
    expires: int | None = None,
) -> dict[str, Any]:
    payload = {
        "sequence": sequence,
        "application_id": application_id or f"app-{sequence:04d}",
        "tenant": tenant,
        "service": service,
        "millicores": mc,
        "mib": mib,
        "requested_at_ms": at,
        "duration_ms": duration,
    }
    if expires is not None:
        payload["expires_at_ms"] = expires
    return payload


def app_doc(*applications: dict[str, Any]) -> dict[str, Any]:
    return {"applications": list(applications)}


def load(
    engine: LedgerEngine,
    tenants: dict[str, Any],
    services: dict[str, Any],
    applications: dict[str, Any],
    *,
    reset: bool = False,
) -> dict[str, Any]:
    from quota_ledger.engine import load_documents

    return load_documents(
        engine,
        tenants,
        services,
        applications,
        reset=reset,
        at_ms=0,
    )


def read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def run_cli(
    db_path: Path,
    *args: str,
    crashpoint: str | None = None,
    timeout: float = 30,
) -> subprocess.CompletedProcess[str]:
    env = None
    if crashpoint is not None:
        import os

        env = os.environ.copy()
        env["QUOTA_LEDGER_CRASH_POINT"] = crashpoint
        env["PYTHONPATH"] = str(ROOT)
    return subprocess.run(
        [
            sys.executable,
            "-m",
            "quota_ledger",
            "--db",
            str(db_path),
            *args,
        ],
        cwd=ROOT,
        text=True,
        capture_output=True,
        timeout=timeout,
        env=env,
    )


def db_rows(db_path: Path, sql: str, params: tuple[Any, ...] = ()) -> list[dict[str, Any]]:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        return [dict(row) for row in conn.execute(sql, params).fetchall()]
    finally:
        conn.close()


def db_one(db_path: Path, sql: str, params: tuple[Any, ...] = ()) -> dict[str, Any]:
    rows = db_rows(db_path, sql, params)
    assert len(rows) == 1
    return rows[0]
