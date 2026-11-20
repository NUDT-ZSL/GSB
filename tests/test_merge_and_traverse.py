from __future__ import annotations

import json
import sqlite3

import pytest

from graphstore.errors import (
    E_CLOCK_ROLLBACK,
    E_INVALID_DEPTH,
    E_INVALID_EDGE_LABEL,
    E_NODE_NOT_FOUND,
    GraphStoreError,
)
from graphstore.model import ClockConfig
from graphstore.storage import GraphStore
from graphstore.traverse import run_traversal


def test_merge_builds_traceable_deterministic_index(loaded_store: GraphStore) -> None:
    index = loaded_store.load_index()
    assert (index.node_count, index.edge_count, index.tombstone_count) == (9, 9, 2)
    assert index.label_prefix("per") == ("n1", "n2", "n3", "n6", "n9")
    assert index.label_prefix("aud") == ("n4",)

    n1 = index.nodes["n1"]
    assert n1.source.format == "v2"
    assert n1.source.snapshot_version == 3
    assert n1.source.shard_generation == 4
    assert n1.decision_reason.startswith("tiered_latest")

    n6 = index.nodes["n6"]
    assert n6.source.format == "v1"
    assert n6.written_at_ms == 1_710_000_005_000

    n3 = index.nodes["n3"]
    assert n3.tombstone is True
    assert index.adjacency.get("n3", ()) == ()

    follows = [
        edge
        for edge in index.adjacency["n1"]
        if edge.to_id == "n2" and edge.label == "follows"
    ][0]
    assert follows.source.format == "v2"
    assert follows.weight == 0.8
    assert len(follows.sources) == 2
    assert {source["source"]["format"] for source in follows.sources} == {"v1", "v2"}

    assert index.adjacency_digest() == (
        "8cf4ed19fc7633b2565b0e0a3bc8fd931d97c34082ef61522004ac994b253080"
    )


def test_exact_and_label_prefix_audit_tombstones(loaded_store: GraphStore) -> None:
    exact = run_traversal(
        loaded_store,
        batch_id="exact-n3",
        query={"kind": "exact", "node_id": "n3"},
    ).result
    assert exact["results"][0]["tombstone"] is True
    assert exact["results"][0]["node_id"] == "n3"
    assert exact["results"][0]["candidates"]

    prefix = run_traversal(
        loaded_store,
        batch_id="prefix-person",
        query={"kind": "label_prefix", "prefix": "person"},
    ).result
    assert [item["node_id"] for item in prefix["results"]] == ["n1", "n2", "n6"]
    assert prefix["stats"]["visited"] == 5

    audit_prefix = run_traversal(
        loaded_store,
        batch_id="prefix-person-audit",
        query={
            "kind": "label_prefix",
            "prefix": "person",
            "include_tombstones": True,
        },
    ).result
    assert [item["node_id"] for item in audit_prefix["results"]] == [
        "n1",
        "n2",
        "n3",
        "n6",
        "n9",
    ]


def test_two_hop_reachability_and_tombstone_truncation(
    loaded_store: GraphStore,
) -> None:
    result = run_traversal(
        loaded_store,
        batch_id="two-hop",
        query={"kind": "reachable", "node_id": "n1", "depth": 2},
    ).result
    assert [(item["node_id"], item["depth"]) for item in result["results"]] == [
        ("n2", 1),
        ("n7", 1),
        ("n4", 2),
    ]
    assert result["stats"] == {"emitted": 3, "truncated": 2, "visited": 4}
    assert all(item["truncated"] is True for item in result["truncations"])
    assert all(item["node_id"] == "n3" for item in result["truncations"])
    assert result["results"][2]["edge_labels"] == ["follows", "blocked"]


def test_edge_filters_apply_per_edge_without_weight_accumulation(
    loaded_store: GraphStore,
) -> None:
    result = run_traversal(
        loaded_store,
        batch_id="filtered",
        query={
            "kind": "filtered",
            "node_id": "n1",
            "depth": 2,
            "edge_labels": ["follows"],
            "min_weight": 0.7,
        },
    ).result
    assert [item["node_id"] for item in result["results"]] == ["n2"]
    assert [item["node_id"] for item in result["truncations"]] == ["n3"]


def test_query_errors_are_distinct(loaded_store: GraphStore) -> None:
    with pytest.raises(GraphStoreError) as error:
        run_traversal(
            loaded_store,
            batch_id="missing",
            query={"kind": "exact", "node_id": "absent"},
        )
    assert error.value.code == E_NODE_NOT_FOUND
    with sqlite3.connect(loaded_store.db_path) as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM traverse_batches WHERE batch_id='missing'"
        ).fetchone()[0] == 0

    with pytest.raises(GraphStoreError) as error:
        run_traversal(
            loaded_store,
            batch_id="bad-depth",
            query={"kind": "filtered", "node_id": "n1", "depth": 0},
        )
    assert error.value.code == E_INVALID_DEPTH

    with pytest.raises(GraphStoreError) as error:
        run_traversal(
            loaded_store,
            batch_id="bad-label",
            query={
                "kind": "filtered",
                "node_id": "n1",
                "depth": 1,
                "edge_labels": [""],
            },
        )
    assert error.value.code == E_INVALID_EDGE_LABEL

    exact = run_traversal(
        loaded_store,
        batch_id="still-works",
        query={"kind": "exact", "node_id": "n1"},
    ).result
    assert exact["results"][0]["node_id"] == "n1"


def test_output_is_bit_stable_across_repetition(loaded_store: GraphStore) -> None:
    query = {"kind": "reachable", "node_id": "n1", "depth": 2}
    first = run_traversal(loaded_store, batch_id="stable", query=query).result
    second = run_traversal(loaded_store, batch_id="stable", query=query).result
    assert first == second
    assert loaded_store.verify("stable")["ok"] is True


def test_traversal_rejects_injected_clock_rollback(
    loaded_store: GraphStore,
) -> None:
    rolled_back = ClockConfig(
        anchor_ms=1_700_000_000_000,
        step_ms=250,
        max_rollback_ms=1_000,
        v1_scale_numerator=1_000,
        v1_scale_denominator=1,
        v1_rounding="floor",
    )
    loaded_store.clock = rolled_back
    with pytest.raises(GraphStoreError) as error:
        run_traversal(
            loaded_store,
            batch_id="clock-rollback-query",
            query={"kind": "exact", "node_id": "n1"},
        )
    assert error.value.code == E_CLOCK_ROLLBACK

