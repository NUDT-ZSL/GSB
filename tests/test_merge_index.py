from __future__ import annotations

from graphstore.index import build_index
from graphstore.merge import merge_snapshots
from graphstore.models import Edge, NodeVersion
from graphstore.merge import _merge_node
from graphstore.snapshot import load_snapshots


def _merged(sample_paths):
    v1, v2, package = load_snapshots(
        sample_paths["v1"],
        sample_paths["v2"],
        logical_clock_ms=sample_paths["logical_clock_ms"],
    )
    return merge_snapshots(v1, v2, package)


def test_newer_v2_node_wins_and_v1_conversion_is_audited(sample_paths):
    merged = _merged(sample_paths)
    n1 = merged.nodes["n1"]
    assert n1.source == "v2:s0001"
    assert n1.label == "person/senior"
    assert n1.logical_clock == 1_700_000_061_000
    n6 = merged.nodes["n6"]
    assert n6.source == "v1"
    assert n6.conversion["raw_written_seconds"] == 6
    assert n6.conversion["pre_round_ms"] == 1_700_000_006_000


def test_tombstone_is_audited_but_not_visible(sample_paths):
    merged = _merged(sample_paths)
    assert "n3" in merged.nodes
    assert merged.nodes["n3"].tombstone is True
    index = build_index(merged, index_version="idx-test")
    assert "n3" not in index.visible_node_ids
    assert index.summary["tombstone_count"] == 1
    assert index.summary["tombstones"][0]["id"] == "n3"


def test_edge_dedupe_sources_and_conflict_trace(sample_paths):
    merged = _merged(sample_paths)
    edge = next(
        item for item in merged.nodes["n1"].edges
        if item.target == "n2" and item.label == "follows"
    )
    assert edge.weight == 0.8
    assert edge.winner_source == "v2:s0001"
    assert edge.conflict is True
    assert edge.sources == ("v1", "v2:s0001")
    assert {item["weight"] for item in edge.candidates} == {0.5, 0.8}
    assert any(
        decision["node_id"] == "n1"
        and decision["target"] == "n2"
        and decision["conflict"]
        for decision in merged.edge_decisions
    )


def test_same_logical_clock_prefers_v2_source(sample_paths):
    v1 = NodeVersion(
        node_id="x",
        label="person/old",
        weight=1.0,
        logical_clock=100,
        generation=0,
        source="v1",
        source_priority=1,
        tombstone=False,
    )
    v2 = NodeVersion(
        node_id="x",
        label="person/new",
        weight=2.0,
        logical_clock=100,
        generation=0,
        source="v2:s0001",
        source_priority=2,
        tombstone=False,
    )
    node, _decision = _merge_node("x", [v1, v2])
    assert node.source == "v2:s0001"
    assert node.label == "person/new"


def test_repeated_index_build_is_byte_stable(sample_paths):
    merged = _merged(sample_paths)
    first = build_index(merged, index_version="idx-stable")
    second = build_index(merged, index_version="idx-stable")
    assert first.digest == second.digest
    assert first.summary == second.summary
    assert first.summary["node_count"] == 11
    assert first.summary["visible_node_count"] == 10
    assert first.summary["edge_count"] == 18
    assert first.summary["label_prefixes"] == sorted(
        first.summary["label_prefixes"]
    )
    assert first.label_prefix_index["person/"] == ("n1", "n2", "n7")
    assert first.target_index["n4"] == ("n1", "n11", "n2")
    assert first.edge_label_index["mentions"] == ("n11", "n4", "n8")

