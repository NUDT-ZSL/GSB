from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys

from quota_ledger.engine import LedgerEngine

from .conftest import app, app_doc, db_one, db_rows, load, tenant_doc


def _seed(engine: LedgerEngine) -> None:
    services = {
        "services": [
            {
                "name": "svc-a",
                "resource_pool": "pool-main",
                "requests_per_round": 50,
                "max_concurrent_reservations": 50,
            },
            {
                "name": "svc-b",
                "resource_pool": "pool-main",
                "requests_per_round": 50,
                "max_concurrent_reservations": 50,
            },
        ]
    }
    load(
        engine,
        tenant_doc(
            quota_mc=1000,
            quota_mib=1000,
            pool_capacity_mc=2000,
            pool_capacity_mib=2000,
        ),
        services,
        app_doc(
            *[
                app(
                    sequence,
                    service="svc-a" if sequence % 2 else "svc-b",
                    mc=100,
                    mib=100,
                    at=sequence - 1,
                    duration=5000,
                )
                for sequence in range(1, 11)
            ]
        ),
        reset=True,
    )


def _spawn(db_path: Path, args: list[str]) -> subprocess.Popen[str]:
    env = os.environ.copy()
    env["PYTHONPATH"] = str(Path(__file__).resolve().parents[1])
    return subprocess.Popen(
        [
            sys.executable,
            "-m",
            "quota_ledger",
            "--db",
            str(db_path),
            *args,
        ],
        cwd=Path(__file__).resolve().parents[1],
        env=env,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )


def test_multiprocess_conservation_and_idempotent_duplicate_submit(
    engine: LedgerEngine,
) -> None:
    _seed(engine)
    processes = [
        _spawn(
            engine.db_path,
            [
                "submit",
                "--application",
                f"app-{sequence:04d}",
                "--now-ms",
                "0",
                "--round-id",
                "multi",
                "--round-start-ms",
                "0",
                "--round-budget-ms",
                "100",
            ],
        )
        for sequence in range(1, 11)
    ]
    # Also race six copies of the same application.
    processes.extend(
        _spawn(
            engine.db_path,
            [
                "submit",
                "--application",
                "app-0001",
                "--now-ms",
                "0",
                "--round-id",
                "multi",
                "--round-start-ms",
                "0",
                "--round-budget-ms",
                "100",
            ],
        )
        for _ in range(6)
    )
    outputs = []
    for process in processes:
        stdout, stderr = process.communicate(timeout=60)
        assert process.returncode == 0, stderr + stdout
        outputs.append(json.loads(stdout))

    assert db_one(
        engine.db_path,
        "SELECT base_used_mc FROM tenants WHERE name = 'tenant-a'",
    )["base_used_mc"] == 1000
    duplicate_admissions = db_rows(
        engine.db_path,
        """
        SELECT application_id, COUNT(*) AS n FROM ledger_entries
        WHERE entry_type = 'QUOTA_ADMIT'
        GROUP BY application_id HAVING COUNT(*) > 1
        """,
    )
    assert duplicate_admissions == []
    assert db_one(
        engine.db_path,
        """
        SELECT COUNT(*) AS n FROM ledger_entries
        WHERE entry_type = 'QUOTA_ADMIT' AND application_id = 'app-0001'
        """,
    )["n"] == 1
    assert engine.verify()["ok"]

    # Concurrent duplicate releases must also be idempotent.
    releases = [
        _spawn(
            engine.db_path,
            [
                "release",
                "--application",
                "app-0001",
                "--now-ms",
                "100",
                "--trigger",
                "MANUAL",
            ],
        )
        for _ in range(5)
    ]
    for process in releases:
        stdout, stderr = process.communicate(timeout=60)
        assert process.returncode == 0, stderr + stdout
    assert db_one(
        engine.db_path,
        """
        SELECT COUNT(*) AS n FROM ledger_entries
        WHERE entry_type = 'QUOTA_RELEASE' AND application_id = 'app-0001'
        """,
    )["n"] == 1
    assert db_one(
        engine.db_path,
        "SELECT COUNT(*) AS n FROM release_events WHERE application_id = 'app-0001'",
    )["n"] == 1
    assert engine.verify()["ok"]
