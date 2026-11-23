from __future__ import annotations

from quota_ledger.engine import LedgerEngine, load_documents
from quota_ledger.errors import BusinessError

from .conftest import ROOT, db_one, db_rows, read_json


def _load_sample(engine: LedgerEngine) -> None:
    load_documents(
        engine,
        read_json(ROOT / "samples" / "tenants.json"),
        read_json(ROOT / "samples" / "services.json"),
        read_json(ROOT / "samples" / "applications.json"),
        reset=True,
    )


def _submit_all_sample(engine: LedgerEngine) -> list[dict]:
    results = []
    for app in engine.list_staged_applications():
        if int(app["requested_at_ms"]) > 100:
            results.append(
                engine.abandon_application(
                    app["application_id"],
                    reason_code="ROUND_BUDGET_EXHAUSTED",
                    at_ms=0,
                    round_id="sample-round",
                    round_start_ms=0,
                    round_budget_ms=100,
                )
            )
        else:
            results.append(
                engine.submit(
                    app["application_id"],
                    round_id="sample-round",
                    round_start_ms=0,
                    round_budget_ms=100,
                )
            )
    return results


def test_sample_parallel_ledger_borrow_queue_and_precision(
    engine: LedgerEngine,
) -> None:
    _load_sample(engine)
    results = _submit_all_sample(engine)
    by_id = {r["application"]["application_id"]: r for r in results}

    assert by_id["app-0016"]["application"]["status"] == "ABANDONED"
    assert by_id["app-0016"]["application"]["reason_code"] == "ROUND_BUDGET_EXHAUSTED"
    assert by_id["app-0006"]["application"]["status"] == "WAITING"
    assert by_id["app-0006"]["application"]["reason_code"] == "TENANT_BORROW_LIMIT"
    assert by_id["app-0014"]["application"]["status"] == "ADMITTED"
    assert by_id["app-0015"]["application"]["status"] == "WAITING"
    assert by_id["app-0005"]["application"]["precision_sensitive"] == 1

    status = engine.status()
    red = next(t for t in status["tenants"] if t["name"] == "team-red")
    assert red["base_used"] == {"millicores": 800, "mib": 1024}
    assert red["borrowed"] == {"millicores": 400, "mib": 56}
    assert red["remaining"] == {"millicores": 0, "mib": 0}
    assert red["precision_sensitive"] is True
    assert any(
        e["application_id"] == "app-0005" and e["sensitive"] == 1
        for e in status["precision_events"]
    )
    chain = next(
        e["chain"] for e in status["precision_events"] if e["application_id"] == "app-0005"
    )
    assert chain["tenant"] == "team-red"
    assert chain["application_sequence"] == 5
    assert chain["steps"]
    assert chain["fixed_scale"] == 1000

    verified = engine.verify()
    assert verified["ok"], verified

    # Idempotent duplicate submit must not deduct again.
    before = db_one(
        engine.db_path,
        "SELECT COUNT(*) AS n FROM ledger_entries WHERE entry_type = 'QUOTA_ADMIT'",
    )["n"]
    duplicate = engine.submit(
        "app-0005",
        now_ms=5000,
        round_id="sample-round",
        round_start_ms=0,
        round_budget_ms=100,
    )
    assert duplicate["duplicate"] is True
    after = db_one(
        engine.db_path,
        "SELECT COUNT(*) AS n FROM ledger_entries WHERE entry_type = 'QUOTA_ADMIT'",
    )["n"]
    assert after == before
    assert engine.verify()["ok"]

    # Expiring app-0001 releases base quota and wakes the stable queue head.
    resumed = engine.resume(now_ms=200)
    assert resumed["ok"]
    expired = next(
        item for item in resumed["due_releases"]
        if item["application"]["application_id"] == "app-0001"
    )
    assert expired["release_event"]["trigger_source_application_id"] == "app-0001"
    assert expired["wake_attempts"][0]["application_id"] == "app-0006"
    assert by_id["app-0006"]["application"]["status"] == "WAITING"
    restored = engine.get_application("app-0006")
    assert restored["application"]["status"] == "ADMITTED"

    # Releasing a borrowing reservation first returns borrow, then base quota.
    released = engine.release("app-0002", at_ms=500, trigger_type="MANUAL")
    assert released["release_event"]["returned_borrow_mc"] == 100
    ledger = db_rows(
        engine.db_path,
        """
        SELECT entry_type, sequence_in_transaction
        FROM ledger_entries WHERE operation_id = ?
        ORDER BY sequence_in_transaction
        """,
        ("release:app-0002:reservation:app-0002",),
    )
    assert [row["entry_type"] for row in ledger] == [
        "BORROW_RETURN",
        "QUOTA_RELEASE",
    ]

    duplicate_release = engine.release("app-0002", at_ms=550, trigger_type="MANUAL")
    assert duplicate_release["duplicate"] is True
    assert engine.verify()["ok"]


def test_borrow_mismatch_rejects_release_with_distinct_code(
    engine: LedgerEngine,
) -> None:
    from .conftest import app, app_doc, load, service_doc, tenant_doc

    load(
        engine,
        tenant_doc(
            quota_mc=100,
            quota_mib=100,
            allow_overdraft=True,
            borrow_mc=100,
            borrow_mib=100,
            pool_capacity_mc=200,
            pool_capacity_mib=200,
        ),
        service_doc(),
        app_doc(
            app(1, mc=100, mib=100, at=0, duration=200),
            app(2, mc=100, mib=100, at=1, duration=200),
            app(3, mc=50, mib=50, at=2, duration=200),
        ),
        reset=True,
    )
    for sequence in (1, 2, 3):
        engine.submit(
            f"app-{sequence:04d}",
            round_id="r",
            round_start_ms=0,
            round_budget_ms=100,
        )
    assert engine.get_application("app-0003")["application"]["status"] == "WAITING"
    import sqlite3

    conn = sqlite3.connect(engine.db_path)
    try:
        conn.execute(
            """
            UPDATE borrow_obligations SET outstanding_mc = 0
            WHERE application_id = 'app-0002'
            """
        )
        conn.commit()
    finally:
        conn.close()

    import pytest

    with pytest.raises(BusinessError) as exc:
        engine.release("app-0002", at_ms=50, trigger_type="MANUAL")
    assert exc.value.code == "RELEASE_BORROW_MISMATCH"
    reservation = db_one(
        engine.db_path,
        "SELECT state FROM reservations WHERE application_id = 'app-0002'",
    )
    assert reservation["state"] == "ACTIVE"
    assert engine.get_application("app-0003")["application"]["status"] == "WAITING"


def test_release_wakes_multiple_stable_queue_candidates(
    engine: LedgerEngine,
) -> None:
    from .conftest import app, app_doc, load, service_doc, tenant_doc

    load(
        engine,
        tenant_doc(quota_mc=100, quota_mib=100),
        service_doc(),
        app_doc(
            app(1, mc=100, mib=100, at=0, duration=10),
            app(2, mc=30, mib=30, at=1, duration=100),
            app(3, mc=40, mib=40, at=2, duration=100),
            app(4, mc=40, mib=40, at=3, duration=100),
        ),
        reset=True,
    )
    for sequence in (1, 2, 3, 4):
        engine.submit(
            f"app-{sequence:04d}",
            round_id="r",
            round_start_ms=0,
            round_budget_ms=100,
        )
    assert engine.get_application("app-0002")["application"]["status"] == "WAITING"
    released = engine.release("app-0001", at_ms=10, trigger_type="EXPIRY")
    assert [a["application_id"] for a in released["wake_attempts"]] == [
        "app-0002",
        "app-0003",
        "app-0004",
    ]
    assert [a["outcome"] for a in released["wake_attempts"]] == [
        "ADMITTED",
        "ADMITTED",
        "BLOCKED",
    ]
    assert [row["application_id"] for row in released["final_queue"]] == [
        "app-0004"
    ]
    assert engine.get_application("app-0002")["application"]["status"] == "ADMITTED"
    assert engine.get_application("app-0003")["application"]["status"] == "ADMITTED"
    assert engine.get_application("app-0004")["application"]["status"] == "WAITING"
    assert engine.verify()["ok"]
