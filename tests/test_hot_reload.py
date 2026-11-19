from __future__ import annotations

import threading

from gateway.config import parse_config

from .conftest import call_gateway, principals, sample_config, update_raw


def test_inflight_request_keeps_its_start_snapshot(service):
    p = principals()
    original = service._handle_locked
    bound = threading.Event()
    release = threading.Event()

    def paused(**kwargs):
        if kwargs["headers"].get("X-Request-Id") == "inflight-v1":
            bound.set()
            assert release.wait(timeout=10)
        return original(**kwargs)

    service._handle_locked = paused
    holder = {}

    def run_old_request():
        holder["response"] = call_gateway(
            service,
            principal=p["api_a_v1"],
            request_id="inflight-v1",
            request_seq=1,
            logical_time_ms=10_000,
        )

    thread = threading.Thread(target=run_old_request)
    thread.start()
    assert bound.wait(timeout=10)

    update_raw(service, sample_config(2), expected=1)
    release.set()
    thread.join(timeout=10)
    assert not thread.is_alive()

    newer = call_gateway(
        service,
        principal=p["api_a_v2"],
        request_id="new-v2",
        request_seq=2,
        logical_time_ms=10_001,
    )
    assert newer.status == 200
    assert newer.config_version == 2
    older = holder["response"]
    assert older.status == 200
    assert older.config_version == 1
    assert older.config_digest == parse_config(sample_config(1)).digest
    assert any(
        item.get("snapshot") == "bound_at_request_start"
        for item in older.decision_chain
    )
    assert service.store.current_version() == 2


def test_new_request_binds_new_snapshot_not_mixed(service):
    update_raw(service, sample_config(2), expected=1)
    p = principals()
    response = call_gateway(
        service,
        principal=p["api_a_v2"],
        request_id="snapshot-v2",
        request_seq=1,
        logical_time_ms=20_000,
    )
    assert response.status == 200
    assert response.config_version == 2
    assert response.config_digest == parse_config(sample_config(2)).digest
    config_stage = next(
        item for item in response.decision_chain if item["stage"] == "config"
    )
    assert config_stage["config_version"] == 2
    assert config_stage["config_digest"] == response.config_digest
    limit_stage = next(
        item for item in response.decision_chain if item["stage"] == "limit"
    )
    assert limit_stage["route_bucket"]["capacity_units"] == 3_000


def test_expected_config_version_mismatch_does_not_consume_tokens(service):
    p = principals()
    before = service.state_snapshot("tenant-a")
    response = call_gateway(
        service,
        principal=p["api_a_v1"],
        request_id="expect-v1",
        request_seq=1,
        logical_time_ms=9_000,
        expected_config_version=2,
    )
    after = service.state_snapshot("tenant-a")
    assert response.status == 409
    assert response.code == "E_CONFIG_VERSION_MISMATCH"
    assert after["token_buckets"] == before["token_buckets"]
    assert after["window_buckets"] == before["window_buckets"]
    assert after["logical_clock_ms"] == before["logical_clock_ms"]


def test_inflight_request_does_not_observe_new_revocation(service):
    p = principals()
    original = service._handle_locked
    bound = threading.Event()
    release = threading.Event()

    def paused(**kwargs):
        if kwargs["headers"].get("X-Request-Id") == "inflight-revocation":
            bound.set()
            assert release.wait(timeout=10)
        return original(**kwargs)

    service._handle_locked = paused
    holder = {}

    def run_old_request():
        holder["response"] = call_gateway(
            service,
            principal=p["api_a_v1"],
            request_id="inflight-revocation",
            request_seq=1,
            logical_time_ms=10_000,
        )

    thread = threading.Thread(target=run_old_request)
    thread.start()
    assert bound.wait(timeout=10)

    raw = sample_config(2)
    raw["auth"]["revocations"].append(
        {
            "type": "api_key",
            "id": "key-a-v1",
            "revoked_at_ms": 10_000,
            "reason": "concurrent hot update",
        }
    )
    update_raw(service, raw, expected=1)
    release.set()
    thread.join(timeout=10)
    assert not thread.is_alive()
    assert holder["response"].status == 200

    revoked_for_new_request = call_gateway(
        service,
        principal=p["api_a_v1"],
        request_id="new-revoked",
        request_seq=2,
        logical_time_ms=10_001,
    )
    assert revoked_for_new_request.status == 403
    assert revoked_for_new_request.code == "E_AUTH_REVOKED"
