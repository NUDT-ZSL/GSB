from __future__ import annotations

import pytest

from quota_ledger.engine import LedgerEngine
from quota_ledger.errors import BusinessError

from .conftest import app, app_doc, db_one, db_rows, load, service_doc, tenant_doc


def test_version_replacement_binds_inflight_reservations(
    engine: LedgerEngine,
) -> None:
    load(
        engine,
        tenant_doc(quota_mc=100, quota_mib=100, version=1),
        service_doc(),
        app_doc(app(1, mc=60, mib=60, at=0, duration=1000)),
        reset=True,
    )
    engine.submit("app-0001", round_id="r", round_start_ms=0, round_budget_ms=10)
    load(
        engine,
        tenant_doc(quota_mc=150, quota_mib=150, version=2),
        service_doc(),
        app_doc(app(1, mc=60, mib=60, at=0, duration=1000)),
    )
    tenant = db_one(
        engine.db_path,
        "SELECT * FROM tenants WHERE name = 'tenant-a'",
    )
    assert tenant["current_version"] == 2
    assert tenant["quota_mc"] == 150
    assert tenant["base_used_mc"] == 60
    assert db_one(
        engine.db_path,
        "SELECT declaration_version FROM reservations WHERE application_id = 'app-0001'",
    )["declaration_version"] == 1
    versions = db_rows(
        engine.db_path,
        """
        SELECT version, classification FROM tenant_versions
        WHERE tenant_name = 'tenant-a' ORDER BY version
        """,
    )
    assert versions == [
        {"version": 1, "classification": "INCREMENTAL_APPEND"},
        {"version": 2, "classification": "FULL_REPLACE"},
    ]
    released = engine.release("app-0001", at_ms=100, trigger_type="MANUAL")
    assert released["release_event"]["declaration_version"] == 1
    assert db_one(
        engine.db_path,
        "SELECT quota_mc FROM tenants WHERE name = 'tenant-a'",
    )["quota_mc"] == 150

    with pytest.raises(BusinessError) as exc:
        load(
            engine,
            tenant_doc(quota_mc=100, quota_mib=100, version=1),
            service_doc(),
            app_doc(app(1, mc=60, mib=60, at=0, duration=1000)),
        )
    assert exc.value.code == "VERSION_ROLLBACK"
    no_op = load(
        engine,
        tenant_doc(quota_mc=150, quota_mib=150, version=2),
        service_doc(),
        app_doc(app(1, mc=60, mib=60, at=0, duration=1000)),
    )
    assert no_op["operation"] == "NO_OP"
    with pytest.raises(BusinessError) as exc:
        load(
            engine,
            tenant_doc(quota_mc=200, quota_mib=150, version=2),
            service_doc(),
            app_doc(app(1, mc=60, mib=60, at=0, duration=1000)),
        )
    assert exc.value.code == "VERSION_CONFLICT"
    assert engine.verify()["ok"]


def test_fixed_point_precision_chain_and_error_spectrum(
    engine: LedgerEngine,
) -> None:
    load(
        engine,
        tenant_doc(quota_mc=100, quota_mib=100),
        service_doc(),
        app_doc(app(1, mc=100, mib=100, at=0, duration=1000)),
        reset=True,
    )
    result = engine.submit(
        "app-0001", round_id="r", round_start_ms=0, round_budget_ms=10
    )
    assert result["application"]["status"] == "ADMITTED"
    assert result["precision_sensitive"] == 1
    status = engine.status()
    tenant = status["tenants"][0]
    assert tenant["precision_sensitive"] is True
    assert tenant["remaining"] == {"millicores": 0, "mib": 0}
    event = status["precision_events"][0]
    assert event["sensitive"] == 1
    assert event["gap_micro"] == 0
    chain = event["chain"]
    assert chain["tenant"] == "tenant-a"
    assert chain["application_sequence"] == 1
    assert chain["fixed_scale"] == 1000
    assert any(step["field"] == "free_quota_mc" for step in chain["steps"])
    assert any(step["field"] == "usage_rate_mc" for step in chain["steps"])
    assert tenant["usage_rate"]["mc"]["rounding"] == "DOWN"
    assert tenant["usage_rate"]["mc"]["error_bound_micro"] == 0
    conversions = db_rows(
        engine.db_path,
        """
        SELECT field, scale, rounding, raw_numerator, raw_denominator,
               converted, error_bound_micro
        FROM conversion_steps WHERE application_id = 'app-0001'
        ORDER BY id
        """,
    )
    assert conversions
    assert all(row["scale"] == 1000 for row in conversions)
