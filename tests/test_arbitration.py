from __future__ import annotations

import itertools

from cdn_edge_workbench.index import arbitrate_candidates
from cdn_edge_workbench.models import CacheEntry
from tests.conftest import digest, etag


def make_entry(
    *,
    node: str,
    version: int,
    clock: int,
    generation: int,
    source_version: str,
    body: str = "value",
) -> CacheEntry:
    return CacheEntry(
        node=node,
        path="/shared",
        digest_algorithm="sha256",
        content_digest=digest(body),
        content_version=version,
        etag=etag(body),
        last_verified_clock=clock,
        expires_at_clock=clock + 10,
        cache_generation=generation,
        source_version=source_version,
        source_ref=f"{source_version}:{node}",
        raw_time={"kind": source_version},
        effective_ttl_seconds=10,
    )


def test_arbitration_is_input_order_independent() -> None:
    candidates = [
        make_entry(
            node="edge-a",
            version=2,
            clock=100,
            generation=2,
            source_version="v2",
        ),
        make_entry(
            node="edge-b",
            version=1,
            clock=100,
            generation=3,
            source_version="v2",
            body="older",
        ),
        make_entry(
            node="edge-c",
            version=2,
            clock=99,
            generation=5,
            source_version="v2",
        ),
    ]
    hashes = {
        arbitrate_candidates("path", "/shared", permutation)["decision_hash"]
        for permutation in itertools.permutations(candidates)
    }
    assert len(hashes) == 1
    decision = arbitrate_candidates("path", "/shared", candidates)
    assert decision["winner"]["node"] == "edge-a"
    assert decision["status"] == "version_divergence"
    assert decision["basis"][0]["criterion"] == "last_verified_clock"


def test_same_clock_prefers_higher_content_version() -> None:
    winner = make_entry(
        node="edge-b",
        version=4,
        clock=50,
        generation=1,
        source_version="v1",
    )
    loser = make_entry(
        node="edge-a",
        version=3,
        clock=50,
        generation=9,
        source_version="v2",
        body="other",
    )
    decision = arbitrate_candidates("path", "/shared", [winner, loser])
    assert decision["winner"]["node"] == "edge-b"
    assert decision["basis"][0]["criterion"] == "content_version"


def test_same_clock_and_version_prefers_v2_format() -> None:
    v2_winner = make_entry(
        node="edge-b",
        version=2,
        clock=50,
        generation=2,
        source_version="v2",
    )
    v1_loser = make_entry(
        node="edge-a",
        version=2,
        clock=50,
        generation=2,
        source_version="v1",
    )
    decision = arbitrate_candidates("path", "/shared", [v1_loser, v2_winner])
    assert decision["winner"]["source_version"] == "v2"
    assert decision["basis"][0]["criterion"] == "source_format_priority"


def test_all_equal_candidates_are_consistent() -> None:
    left = make_entry(
        node="edge-a",
        version=2,
        clock=50,
        generation=2,
        source_version="v2",
    )
    right = make_entry(
        node="edge-b",
        version=2,
        clock=50,
        generation=2,
        source_version="v2",
    )
    decision = arbitrate_candidates("path", "/shared", [left, right])
    assert decision["status"] == "consistent"
    assert decision["basis"][0]["criterion"] == "all_candidates_equal"
    assert decision["differences"] == {}

