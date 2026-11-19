from __future__ import annotations

from .conftest import call_gateway, modify_config, principals, sample_config, update_raw


def test_api_key_rotation_effective_time_and_grace(service):
    p = principals()
    before = call_gateway(
        service,
        principal=p["api_a_v1"],
        request_id="rot-1",
        request_seq=1,
        logical_time_ms=9_999,
    )
    assert before.status == 200

    active = call_gateway(
        service,
        principal=p["api_a_v2"],
        request_id="rot-2",
        request_seq=2,
        logical_time_ms=10_000,
    )
    assert active.status == 200

    old_in_grace = call_gateway(
        service,
        principal=p["api_a_v1"],
        request_id="rot-3",
        request_seq=3,
        logical_time_ms=14_999,
    )
    assert old_in_grace.status == 200

    old_after_grace = call_gateway(
        service,
        principal=p["api_a_v1"],
        request_id="rot-4",
        request_seq=4,
        logical_time_ms=15_000,
    )
    assert old_after_grace.status == 401
    assert old_after_grace.code == "E_AUTH_KEY_RETIRED"


def test_jwt_signature_rotation_and_revocation(service):
    p = principals()
    active = call_gateway(
        service,
        principal=p["jwt_a_v2"],
        request_id="jwt-1",
        request_seq=1,
        logical_time_ms=10_000,
        path="/v1/inventory",
    )
    assert active.status == 200
    assert any(
        stage["stage"] == "auth" and stage["key_version"] == 2
        for stage in active.decision_chain
    )

    revoked = call_gateway(
        service,
        principal=p["jwt_revoked"],
        request_id="jwt-2",
        request_seq=2,
        logical_time_ms=10_001,
    )
    assert revoked.status == 403
    assert revoked.code == "E_AUTH_REVOKED"

    tampered = dict(p["jwt_a_v2"])
    tampered["value"] = p["jwt_a_v2"]["value"][:-4] + "AAAA"
    invalid = call_gateway(
        service,
        principal=tampered,
        request_id="jwt-3",
        request_seq=3,
        logical_time_ms=10_002,
    )
    assert invalid.status == 401
    assert invalid.code == "E_AUTH_INVALID"


def test_old_jwt_after_rotation_grace_is_rejected(service):
    p = principals()
    response = call_gateway(
        service,
        principal=p["jwt_a_v1"],
        request_id="jwt-old",
        request_seq=1,
        logical_time_ms=15_001,
    )
    assert response.status == 401
    assert response.code == "E_AUTH_KEY_RETIRED"


def test_authentication_failure_does_not_consume_tokens_or_window(service):
    before = service.state_snapshot("tenant-a")
    p = principals()
    bad = dict(p["api_a_v2"])
    bad["value"] = "key-a-v2." + "0" * 64
    response = call_gateway(
        service,
        principal=bad,
        request_id="bad-auth",
        request_seq=1,
        logical_time_ms=10_000,
    )
    assert response.status == 401
    assert response.code == "E_AUTH_INVALID"
    after = service.state_snapshot("tenant-a")
    assert after["token_buckets"] == before["token_buckets"]
    assert after["window_buckets"] == before["window_buckets"]


def test_limited_request_records_auth_and_limit_stages(service):
    p = principals()
    responses = []
    for index in range(7):
        responses.append(
            call_gateway(
                service,
                principal=p["api_a_v2"],
                request_id=f"limit-{index}",
                request_seq=index + 1,
                logical_time_ms=20_000,
            )
        )
    assert [response.status for response in responses[:6]] == [200] * 6
    limited = responses[-1]
    assert limited.status == 429
    assert limited.code == "E_RATE_LIMITED"
    stages = {item["stage"]: item for item in limited.decision_chain}
    assert stages["auth"]["decision"] == "allow"
    limit_stage = next(
        item
        for item in limited.decision_chain
        if item["stage"] == "limit" and "tenant_bucket" in item
    )
    assert limit_stage["decision"] == "deny"
    assert limit_stage["tenant_bucket"]["tokens_after_refill_units"] == 0
    assert limited.decision_chain[-1]["code"] == "E_RATE_LIMITED"


def test_revocation_is_sticky_across_config_updates(service):
    raw = modify_config(2)
    raw["version"] = 3
    raw["auth"]["revocations"] = []
    raw["auth"]["api_key_versions"].append(
        {
            "key_id": "key-revoked-1",
            "secret": "revoked-secret",
            "version": 2,
            "tenant": "tenant-a",
            "not_before_ms": 0,
            "expires_at_ms": 120000,
        }
    )
    update_raw(service, raw, expected=1)
    p = principals()

    revoked_api = call_gateway(
        service,
        principal={
            "kind": "api_key",
            "value": (
                "key-revoked-1."
                "a6846bb9b67b616f2b72b5f4a3c0c79dabbc5268118497b50e24f4a28cac5c56"
            ),
            "tenant": "tenant-a",
        },
        request_id="sticky-api",
        request_seq=1,
        logical_time_ms=20_000,
    )
    assert revoked_api.status == 403
    assert revoked_api.code == "E_AUTH_REVOKED"

    revoked_jwt = call_gateway(
        service,
        principal=p["jwt_revoked"],
        request_id="sticky-jwt",
        request_seq=2,
        logical_time_ms=20_000,
    )
    assert revoked_jwt.status == 403
    assert revoked_jwt.code == "E_AUTH_REVOKED"


def test_tenant_hint_mismatch_is_forbidden(service):
    p = principals()
    response = call_gateway(
        service,
        principal=p["api_a_v2"],
        request_id="tenant-mismatch",
        request_seq=1,
        logical_time_ms=20_000,
        extra_headers={"X-Tenant-Id": "tenant-b"},
    )
    assert response.status == 403
    assert response.code == "E_AUTH_FORBIDDEN"


def test_api_key_and_jwt_expiry_use_injected_clock(service):
    p = principals()
    expired_api = call_gateway(
        service,
        principal=p["api_a_v2"],
        request_id="expired-api",
        request_seq=1,
        logical_time_ms=120_000,
    )
    assert expired_api.status == 401
    assert expired_api.code == "E_AUTH_EXPIRED"

    expired_jwt = call_gateway(
        service,
        principal=p["jwt_a_v2"],
        request_id="expired-jwt",
        request_seq=2,
        logical_time_ms=120_000,
    )
    assert expired_jwt.status == 401
    assert expired_jwt.code == "E_AUTH_EXPIRED"
