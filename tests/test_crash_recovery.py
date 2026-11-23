from __future__ import annotations

from pathlib import Path

from quota_ledger.engine import LedgerEngine

from .conftest import (
    app,
    app_doc,
    db_one,
    db_rows,
    load,
    run_cli,
    service_doc,
    tenant_doc,
)


def _seed(engine: LedgerEngine) -> None:
    load(
        engine,
        tenant_doc(quota_mc=100, quota_mib=100),
        service_doc(),
        app_doc(app(1, mc=60, mib=60, at=0, duration=100)),
        reset=True,
    )


def _normalized(db_path: Path) -> dict:
    return {
        "tenant": db_one(
            db_path,
            """
            SELECT base_used_mc, base_used_mib, borrowed_mc, borrowed_mib,
                   quota_mc, quota_mib, current_version, current_digest
            FROM tenants WHERE name = 'tenant-a'
            """,
        ),
        "reservations": db_rows(
            db_path,
            """
            SELECT application_id, base_mc, base_mib, borrow_mc, borrow_mib,
                   state, declaration_version
            FROM reservations ORDER BY application_id
            """,
        ),
        "queue": db_rows(
            db_path,
            """
            SELECT application_id, tenant, sequence, reason_code
            FROM wait_queue ORDER BY tenant, requested_at_ms, sequence
            """,
        ),
        "borrow": db_rows(
            db_path,
            """
            SELECT application_id, outstanding_mc, outstanding_mib, state
            FROM borrow_obligations ORDER BY application_id
            """,
        ),
        "ledger": db_rows(
            db_path,
            """
            SELECT application_id, entry_type, amount_mc, amount_mib,
                   base_used_after_mc, base_used_after_mib,
                   borrowed_after_mc, borrowed_after_mib,
                   sequence_in_transaction
            FROM ledger_entries ORDER BY id
            """,
        ),
    }


def test_admit_mid_transaction_crash_recovers_identically(tmp_path: Path) -> None:
    baseline = LedgerEngine(tmp_path / "baseline.db")
    _seed(baseline)
    baseline.submit("app-0001", now_ms=0, round_id="r", round_start_ms=0, round_budget_ms=100)
    baseline_normalized = _normalized(baseline.db_path)

    crashed = LedgerEngine(tmp_path / "crashed.db")
    _seed(crashed)
    proc = run_cli(
        crashed.db_path,
        "submit",
        "--application",
        "app-0001",
        "--now-ms",
        "0",
        "--round-id",
        "r",
        "--round-start-ms",
        "0",
        "--round-budget-ms",
        "100",
        crashpoint="during_admit_apply",
    )
    assert proc.returncode == 99, proc.stderr
    op = db_one(
        crashed.db_path,
        "SELECT state FROM operations WHERE kind = 'ADMIT'",
    )
    assert op["state"] == "PREPARED"
    assert db_one(
        crashed.db_path,
        "SELECT base_used_mc FROM tenants WHERE name = 'tenant-a'",
    )["base_used_mc"] == 0

    resumed = crashed.resume(now_ms=0)
    assert resumed["ok"]
    assert _normalized(crashed.db_path) == baseline_normalized
    assert db_one(
        crashed.db_path,
        "SELECT combined_digest FROM snapshots ORDER BY id DESC LIMIT 1",
    ) == db_one(
        baseline.db_path,
        "SELECT combined_digest FROM snapshots ORDER BY id DESC LIMIT 1",
    )
    assert crashed.verify()["ok"]


def test_release_mid_transaction_crash_recovers_once(tmp_path: Path) -> None:
    baseline = LedgerEngine(tmp_path / "baseline-release.db")
    _seed(baseline)
    baseline.submit("app-0001", now_ms=0, round_id="r", round_start_ms=0, round_budget_ms=100)
    baseline.release("app-0001", at_ms=50, trigger_type="MANUAL")
    baseline_normalized = _normalized(baseline.db_path)

    crashed = LedgerEngine(tmp_path / "crashed-release.db")
    _seed(crashed)
    submitted = run_cli(
        crashed.db_path,
        "submit",
        "--application",
        "app-0001",
        "--now-ms",
        "0",
        "--round-id",
        "r",
        "--round-start-ms",
        "0",
        "--round-budget-ms",
        "100",
    )
    assert submitted.returncode == 0, submitted.stderr
    proc = run_cli(
        crashed.db_path,
        "release",
        "--application",
        "app-0001",
        "--now-ms",
        "50",
        "--trigger",
        "MANUAL",
        crashpoint="during_release_apply",
    )
    assert proc.returncode == 99, proc.stderr
    event = db_one(
        crashed.db_path,
        "SELECT state FROM release_events WHERE application_id = 'app-0001'",
    )
    assert event["state"] == "PREPARED"
    reservation = db_one(
        crashed.db_path,
        "SELECT state FROM reservations WHERE application_id = 'app-0001'",
    )
    assert reservation["state"] == "ACTIVE"
    assert db_one(
        crashed.db_path,
        "SELECT base_used_mc FROM tenants WHERE name = 'tenant-a'",
    )["base_used_mc"] == 60

    resumed = crashed.resume(now_ms=50)
    assert resumed["ok"]
    assert _normalized(crashed.db_path) == baseline_normalized
    assert db_one(
        crashed.db_path,
        "SELECT combined_digest FROM snapshots ORDER BY id DESC LIMIT 1",
    ) == db_one(
        baseline.db_path,
        "SELECT combined_digest FROM snapshots ORDER BY id DESC LIMIT 1",
    )
    release_rows = db_rows(
        crashed.db_path,
        "SELECT * FROM ledger_entries WHERE entry_type = 'QUOTA_RELEASE'",
    )
    assert len(release_rows) == 1
    assert crashed.verify()["ok"]
