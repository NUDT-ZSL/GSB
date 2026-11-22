from __future__ import annotations

from cdn_edge_workbench.index import (
    choose_validation_mode,
    evict_node,
    freshness_at,
    sampled_buckets,
)
from cdn_edge_workbench.models import CacheEntry, NodeConfig
from tests.conftest import digest, etag


def entry(
    path: str,
    *,
    clock: int,
    expires: int,
    node: str = "edge-a",
    body: str = "body",
    generation: int = 1,
) -> CacheEntry:
    return CacheEntry(
        node=node,
        path=path,
        digest_algorithm="sha256",
        content_digest=digest(body),
        content_version=1,
        etag=etag(body),
        last_verified_clock=clock,
        expires_at_clock=expires,
        cache_generation=generation,
        source_version="v2",
        source_ref="test",
        raw_time={"kind": "test"},
        effective_ttl_seconds=max(0, expires - clock),
    )


def test_fresh_boundary_and_expired_are_distinct() -> None:
    item = entry("/a", clock=100, expires=105)
    assert freshness_at(item, 100, 2).status == "fresh"
    assert freshness_at(item, 103, 2).status == "boundary"
    assert freshness_at(item, 105, 2).status == "boundary"
    assert freshness_at(item, 107, 2).status == "boundary"
    assert freshness_at(item, 108, 2).status == "expired"


def test_sampling_is_deterministic_and_reports_bound() -> None:
    entries = [
        entry(f"/{index}", clock=10, expires=20, body=f"body-{index}")
        for index in range(10)
    ]
    first = sampled_buckets(entries, fraction=0.3, seed="seed")
    second = sampled_buckets(list(reversed(entries)), fraction=0.3, seed="seed")
    assert first[1] == second[1]
    record, selected = choose_validation_mode(
        scope="node",
        target="edge-a",
        item_count=10,
        candidate_count=10,
        threshold=3,
        fraction=0.3,
        seed="seed",
        entries=entries,
    )
    assert record["mode"] == "sampled_digest_buckets"
    assert record["triggered"] is True
    assert record["rejected_mode"] == "exact_per_entry"
    assert record["estimated_false_negative_upper_bound"] == 0.7
    assert record["observation_source"].startswith("content_digest buckets")
    assert selected


def test_exact_mode_is_used_below_threshold() -> None:
    entries = [entry("/a", clock=1, expires=2)]
    record, selected = choose_validation_mode(
        scope="node",
        target="edge-a",
        item_count=1,
        candidate_count=1,
        threshold=3,
        fraction=0.3,
        seed="seed",
        entries=entries,
    )
    assert record["mode"] == "exact_per_entry"
    assert record["estimated_false_negative_upper_bound"] == 0.0
    assert record["rejected_mode"] == "sampled_digest_buckets"
    assert selected == entries


def test_eviction_prefers_expired_then_oldest_clock() -> None:
    node = NodeConfig(
        name="edge-a",
        region="test",
        capacity=2,
        validation_budget=2,
    )
    expired = entry("/expired", clock=1, expires=2)
    old_fresh = entry("/old", clock=4, expires=100, body="old")
    new_fresh = entry("/new", clock=9, expires=100, body="new")
    kept, records, retention = evict_node(
        node,
        [new_fresh, expired, old_fresh],
        logical_clock=10,
        tolerance=0,
    )
    assert {item.path for item in kept} == {"/old", "/new"}
    assert [record["path"] for record in records] == ["/expired"]
    assert records[0]["reason"] == "expired_first"
    assert len(retention) == 2

