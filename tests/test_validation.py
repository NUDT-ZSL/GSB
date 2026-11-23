from __future__ import annotations

import copy

import pytest

from quota_ledger.engine import LedgerEngine, load_documents
from quota_ledger.errors import BusinessError
from quota_ledger.models import parse_bundles

from .conftest import ROOT, app, app_doc, db_one, run_cli, service_doc, tenant_doc


def test_malformed_inputs_do_not_persist(
    engine: LedgerEngine,
) -> None:
    valid_tenants = tenant_doc()
    bad_apps = app_doc(
        app(1, at=100, duration=50, expires=50),
    )
    with pytest.raises(BusinessError) as exc:
        load_documents(engine, valid_tenants, service_doc(), bad_apps)
    assert exc.value.code == "APPLICATION_EXPIRY_BEFORE_REQUEST"
    status = engine.status()
    assert status["tenants"] == []
    assert status["applications"] == []
    assert status["resource_pools"] == []


def test_duplicate_tenant_and_negative_borrow_rejected(
    engine: LedgerEngine,
) -> None:
    tenants = tenant_doc()
    duplicate = copy.deepcopy(tenants["tenants"][0])
    duplicate["name"] = "tenant-a"
    tenants["tenants"].append(duplicate)
    with pytest.raises(BusinessError) as exc:
        load_documents(engine, tenants, service_doc(), app_doc())
    assert exc.value.code == "TENANT_DUPLICATE"
    assert engine.status()["tenants"] == []

    negative = tenant_doc(allow_overdraft=True, borrow_mc=-1)
    with pytest.raises(BusinessError) as exc:
        load_documents(engine, negative, service_doc(), app_doc())
    assert exc.value.code == "INVALID_FIELD_VALUE"
    assert engine.status()["tenants"] == []


def test_resource_pool_closure_and_sequence_validation() -> None:
    tenants = tenant_doc()
    services = service_doc()
    services["services"][0]["resource_pool"] = "missing-pool"
    with pytest.raises(BusinessError) as exc:
        parse_bundles(tenants, services, app_doc())
    assert exc.value.code == "RESOURCE_POOL_UNKNOWN"

    with pytest.raises(BusinessError) as exc:
        parse_bundles(
            tenants,
            service_doc(),
            app_doc(app(2), app(1)),
        )
    assert exc.value.code == "APPLICATION_SEQUENCE_ORDER"


def test_rollback_rejected_without_any_change(
    engine: LedgerEngine,
) -> None:
    from .conftest import load

    load(
        engine,
        tenant_doc(version=2, quota_mc=200),
        service_doc(),
        app_doc(app(1)),
        reset=True,
    )
    before = db_one(engine.db_path, "SELECT * FROM tenants WHERE name = 'tenant-a'")
    with pytest.raises(BusinessError) as exc:
        load(
            engine,
            tenant_doc(version=1, quota_mc=100),
            service_doc(),
            app_doc(app(1)),
        )
    assert exc.value.code == "VERSION_ROLLBACK"
    after = db_one(engine.db_path, "SELECT * FROM tenants WHERE name = 'tenant-a'")
    assert before == after
    versions = db_one(
        engine.db_path,
        "SELECT COUNT(*) AS n FROM tenant_versions WHERE tenant_name = 'tenant-a'",
    )
    assert versions["n"] == 1


def test_cli_parse_error_does_not_create_database(tmp_path) -> None:
    db_path = tmp_path / "never-created.db"
    process = run_cli(
        db_path,
        "load",
        "--tenants",
        str(ROOT / "samples" / "invalid" / "tenants_duplicate.json"),
        "--services",
        str(ROOT / "samples" / "services.json"),
        "--applications",
        str(ROOT / "samples" / "applications.json"),
        "--reset",
    )
    assert process.returncode == 1
    assert '"code": "TENANT_DUPLICATE"' in process.stdout
    assert not db_path.exists()
