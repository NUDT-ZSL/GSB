from __future__ import annotations

import multiprocessing

from gateway.service import GatewayService

from .conftest import SAMPLES, call_gateway, principals
from .multiprocess_worker import queue_target


def _token_map(state):
    return {
        (item["bucket_kind"], item["bucket_key"]): item["tokens_units"]
        for item in state["token_buckets"]
    }


def test_duplicate_request_returns_same_result_without_double_charge(service):
    p = principals()
    first = call_gateway(
        service,
        principal=p["api_a_v2"],
        request_id="idem-1",
        request_seq=1,
        logical_time_ms=20_000,
    )
    state_after_first = service.state_snapshot("tenant-a")
    second = call_gateway(
        service,
        principal=p["api_a_v2"],
        request_id="idem-1",
        request_seq=1,
        logical_time_ms=20_000,
    )
    state_after_second = service.state_snapshot("tenant-a")
    assert first.status == second.status == 200
    assert first.body == second.body
    assert any(item["stage"] == "idempotency" for item in second.decision_chain)
    assert state_after_second["token_buckets"] == state_after_first["token_buckets"]
    assert state_after_second["window_buckets"] == state_after_first["window_buckets"]
    assert sum(item["attempts"] for item in state_after_second["window_buckets"]) == 1


def test_same_request_id_with_different_digest_is_rejected(service):
    p = principals()
    first = call_gateway(
        service,
        principal=p["api_a_v2"],
        request_id="idem-digest",
        request_seq=1,
        logical_time_ms=20_000,
        body=b"first",
    )
    assert first.status == 200
    before = service.state_snapshot("tenant-a")
    mismatch = call_gateway(
        service,
        principal=p["api_a_v2"],
        request_id="idem-digest",
        request_seq=2,
        logical_time_ms=20_001,
        body=b"second",
    )
    after = service.state_snapshot("tenant-a")
    assert mismatch.status == 409
    assert mismatch.code == "E_REQUEST_DIGEST_MISMATCH"
    assert after["token_buckets"] == before["token_buckets"]
    assert after["window_buckets"] == before["window_buckets"]


def test_reused_sequence_with_different_id_is_rejected(service):
    p = principals()
    first = call_gateway(
        service,
        principal=p["api_a_v2"],
        request_id="seq-owner",
        request_seq=1,
        logical_time_ms=20_000,
    )
    assert first.status == 200
    before = service.state_snapshot("tenant-a")
    conflict = call_gateway(
        service,
        principal=p["api_a_v2"],
        request_id="seq-other",
        request_seq=1,
        logical_time_ms=20_001,
    )
    after = service.state_snapshot("tenant-a")
    assert conflict.code == "E_REQUEST_SEQUENCE_CONFLICT"
    assert after["token_buckets"] == before["token_buckets"]
    assert after["window_buckets"] == before["window_buckets"]


def test_parallel_workers_apply_identical_request_once(
    service, database_path, upstream_server
):
    context = multiprocessing.get_context("spawn")
    queue = context.Queue()
    processes = [
        context.Process(
            target=queue_target,
            args=(
                queue,
                str(database_path),
                str(SAMPLES / "config_v1.json"),
                "multi-same",
                1,
                20_000,
            ),
        )
        for _ in range(5)
    ]
    for process in processes:
        process.start()
    for process in processes:
        process.join(timeout=30)
        assert process.exitcode == 0
    workers = [queue.get(timeout=5) for _ in processes]
    assert all("error" not in item for item in workers)
    assert {item["status"] for item in workers} == {502}
    assert {item["body"] for item in workers} == {
        workers[0]["body"],
    }
    state = service.state_snapshot("tenant-a")
    assert sum(item["attempts"] for item in state["window_buckets"]) == 1
    tokens = _token_map(state)
    assert tokens[("route", "down")] == 2_000
    assert tokens[("tenant", "tenant-a")] == 5_000


def test_parallel_workers_do_not_overdraw_route_bucket(
    service, database_path, upstream_server
):
    context = multiprocessing.get_context("spawn")
    queue = context.Queue()
    processes = [
        context.Process(
            target=queue_target,
            args=(
                queue,
                str(database_path),
                str(SAMPLES / "config_v1.json"),
                f"multi-seq-{index}",
                index + 1,
                20_000,
            ),
        )
        for index in range(6)
    ]
    for process in processes:
        process.start()
    for process in processes:
        process.join(timeout=30)
        assert process.exitcode == 0
    workers = [queue.get(timeout=5) for _ in processes]
    assert all("error" not in item for item in workers)
    admitted = [item for item in workers if item["status"] == 502]
    limited = [item for item in workers if item["status"] == 429]
    assert len(admitted) == 3
    assert len(limited) == 3
    assert all(item["code"] == "E_RATE_LIMITED" for item in limited)
    state = service.state_snapshot("tenant-a")
    tokens = _token_map(state)
    assert tokens[("route", "down")] == 0
    assert sum(item["attempts"] for item in state["window_buckets"]) == 6
