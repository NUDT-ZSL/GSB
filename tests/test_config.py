from __future__ import annotations

import copy
import sqlite3

import pytest

from gateway.config import content_digest, parse_config
from gateway.errors import GatewayError

from .conftest import modify_config, sample_config, update_raw


def assert_config_error(raw, code):
    with pytest.raises(GatewayError) as caught:
        parse_config(raw)
    assert caught.value.code == code


def test_valid_samples_parse_and_digest_is_stable():
    first = parse_config(sample_config(1))
    second = parse_config(copy.deepcopy(sample_config(1)))
    assert first.digest == second.digest
    assert first.version == 1
    assert len(first.routes) == 3


def test_duplicate_route_host_and_prefix_is_rejected():
    raw = modify_config()
    raw["routes"][1]["id"] = "inventory-copy"
    raw["routes"][1]["host"] = "api.local"
    raw["routes"][1]["path_prefix"] = "/v1/orders"
    raw["limits"]["route_buckets"]["inventory-copy"] = raw["limits"]["route_buckets"].pop(
        "inventory"
    )
    raw["auth"]["permission_matrix"]["inventory.read"] = ["inventory-copy"]
    assert_config_error(raw, "E_BAD_ROUTE")


def test_non_loopback_upstream_is_rejected():
    raw = modify_config()
    raw["routes"][0]["upstream"] = "http://10.1.2.3:18081"
    assert_config_error(raw, "E_BAD_ROUTE")


@pytest.mark.parametrize(
    "mutator",
    [
        lambda raw: raw["limits"]["sliding_window"].update({"step_ms": 1001}),
        lambda raw: raw["limits"]["route_buckets"]["orders"].update({"capacity": 0}),
        lambda raw: raw["limits"]["tenant_buckets"]["tenant-a"].update(
            {"refill_per_second": 0}
        ),
    ],
)
def test_limit_validation_is_strict(mutator):
    raw = modify_config()
    mutator(raw)
    assert_config_error(raw, "E_BAD_LIMIT")


def test_permission_matrix_reference_closure():
    raw = modify_config()
    raw["auth"]["permission_matrix"]["orders.read"] = ["missing-route"]
    assert_config_error(raw, "E_BAD_AUTH_MATRIX")


def test_tenant_grant_reference_closure():
    raw = modify_config()
    raw["auth"]["tenant_grants"]["tenant-a"].append("unknown.domain")
    assert_config_error(raw, "E_BAD_AUTH_MATRIX")


def test_revocation_type_is_validated():
    raw = modify_config()
    raw["auth"]["revocations"][0]["type"] = "password"
    assert_config_error(raw, "E_BAD_REVOCATION")


def test_api_key_versions_must_be_monotonic():
    raw = modify_config()
    raw["auth"]["api_key_versions"][0]["version"] = 2
    raw["auth"]["api_key_versions"][1]["version"] = 1
    assert_config_error(raw, "E_BAD_AUTH_KEY")


def test_bad_config_does_not_persist_any_config_or_revocation(service, database_path):
    before_meta = service.store.config_meta()
    before_revocations = service.state_snapshot()["revocations"]
    raw = modify_config(2)
    raw["version"] = 99
    raw["routes"][0]["upstream"] = "http://192.0.2.10:18081"
    raw["auth"]["revocations"].append(
        {"type": "jwt", "id": "must-not-persist", "revoked_at_ms": 0}
    )
    with pytest.raises(GatewayError) as caught:
        update_raw(service, raw, expected=1)
    assert caught.value.code == "E_BAD_ROUTE"
    assert service.store.config_meta() == before_meta
    after = service.state_snapshot()
    assert after["revocations"] == before_revocations
    with sqlite3.connect(database_path) as conn:
        assert conn.execute(
            "SELECT COUNT(*) FROM configs WHERE version = 99"
        ).fetchone()[0] == 0


def test_digest_mismatch_is_rejected_without_switch(service):
    raw = sample_config(2)
    before = service.store.config_meta()
    with pytest.raises(GatewayError) as caught:
        update_raw(service, raw, expected=1, digest="0" * 64)
    assert caught.value.code == "E_CONFIG_DIGEST_MISMATCH"
    assert service.store.config_meta() == before


def test_expected_current_version_must_match(service):
    with pytest.raises(GatewayError) as caught:
        update_raw(service, sample_config(2), expected=7)
    assert caught.value.code == "E_CONFIG_VERSION_MISMATCH"
    assert service.store.current_version() == 1


def test_version_rollback_is_rejected(service):
    update_raw(service, sample_config(2), expected=1)
    with pytest.raises(GatewayError) as caught:
        update_raw(service, sample_config(1), expected=2)
    assert caught.value.code == "E_CONFIG_ROLLBACK"
    assert service.store.current_version() == 2


def test_key_version_watermark_cannot_regress(service):
    raw = modify_config(2)
    raw["version"] = 3
    raw["auth"]["api_key_versions"] = [
        {
            "key_id": "key-a-v1",
            "secret": "a1-secret",
            "version": 1,
            "tenant": "tenant-a",
            "not_before_ms": 0,
            "expires_at_ms": 120000,
        },
        {
            "key_id": "key-b-v1",
            "secret": "b1-secret",
            "version": 1,
            "tenant": "tenant-b",
            "not_before_ms": 0,
            "expires_at_ms": 120000,
        },
    ]
    raw["auth"]["rotation"]["api_key"]["active_version"] = 1
    update_raw(service, sample_config(2), expected=1)
    with pytest.raises(GatewayError) as caught:
        update_raw(service, raw, expected=2)
    assert caught.value.code == "E_AUTH_VERSION_REGRESSION"
    assert service.store.current_version() == 2


def test_admin_api_reports_business_errors(app_client):
    raw = modify_config(2)
    raw["limits"]["sliding_window"]["step_ms"] = 2000
    response = app_client.post(
        "/__admin/config",
        json={
            "config": raw,
            "expected_current_version": 1,
            "content_digest": content_digest(raw),
        },
    )
    assert response.status_code == 400
    assert response.get_json()["code"] == "E_BAD_LIMIT"


def test_admin_api_rejects_malformed_json_as_business_error(app_client):
    response = app_client.post(
        "/__admin/config",
        data=b"{not-json",
        content_type="application/json",
    )
    assert response.status_code == 400
    assert response.get_json()["code"] == "E_BAD_REQUEST"
