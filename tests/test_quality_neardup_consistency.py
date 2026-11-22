from __future__ import annotations

import pytest

from prefworkbench.consistency import compute_consistency
from prefworkbench.errors import (
    E_GROUP_DIRECTION_CONFLICT,
    E_IDENTICAL_SIDE_CONTRADICTION,
    E_LABEL_CROSS_CONTRADICTION,
)
from prefworkbench.generator import default_config, generate_pairs
from prefworkbench.neardup import compute_neardup, pair_signature
from prefworkbench.quality import score_pair


def test_quality_is_deterministic_and_components_are_explicit() -> None:
    config = default_config()["quality"]
    pair = generate_pairs(count=2)[0]
    first = score_pair(pair, config)
    second = score_pair(pair, config)
    assert first == second
    for side in ("chosen", "rejected"):
        component_total = sum(
            first[side]["components"][name]["contribution"]
            for name in ("length", "repetition", "noise")
        )
        assert abs(component_total - first[side]["score"]) < 1e-7
    assert isinstance(first["pass"], bool)
    assert first["quality_tier"] in {"A", "B", "C"}


def test_neardup_partitions_pairs_and_uses_content_signature() -> None:
    config = default_config()["neardup"]
    base = "clear grounded useful safe direct factual complete respectful specific relevant"
    pairs = [
        {
            "pair_id": "P00001",
            "prompt": "same prompt",
            "chosen": base,
            "rejected": base + " extra",
            "annotator_group": "G1",
            "quality_flag": "clean",
        },
        {
            "pair_id": "P00002",
            "prompt": "same prompt",
            "chosen": base,
            "rejected": base + " extra",
            "annotator_group": "G2",
            "quality_flag": "clean",
        },
        {
            "pair_id": "P00003",
            "prompt": "another prompt",
            "chosen": "completely different token stream for uniqueness",
            "rejected": "nothing overlaps in this response at all",
            "annotator_group": "G3",
            "quality_flag": "noisy",
        },
    ]
    quality = {
        "P00001": {"pair_score": 0.9},
        "P00002": {"pair_score": 0.8},
        "P00003": {"pair_score": 0.7},
    }
    result = compute_neardup(
        pairs,
        quality,
        config,
        candidate_mode="all",
        comparison_limit=10,
    )
    assert set(result["assignments"]) == {"P00001", "P00002", "P00003"}
    members = [member for cluster in result["clusters"] for member in cluster["members"]]
    assert sorted(members) == ["P00001", "P00002", "P00003"]
    reps = [cluster["representative_pair_id"] for cluster in result["clusters"]]
    assert len(reps) == len(set(reps))
    duplicate_cluster = next(
        cluster for cluster in result["clusters"] if "P00001" in cluster["members"]
    )
    assert duplicate_cluster["representative_pair_id"] == "P00001"
    assert set(pair_signature(pairs[0], config["chunk_tokens"])[1]) == set(
        pair_signature(pairs[1], config["chunk_tokens"])[1]
    )


def test_consistency_reports_cross_group_and_group_direction_conflicts() -> None:
    config = default_config()["consistency"]
    pairs = [
        {
            "pair_id": "P00001",
            "prompt": "p1",
            "chosen": "alpha",
            "rejected": "beta",
            "annotator_group": "G1",
            "quality_flag": "clean",
        },
        {
            "pair_id": "P00002",
            "prompt": "p1",
            "chosen": "beta",
            "rejected": "alpha",
            "annotator_group": "G1",
            "quality_flag": "clean",
        },
        {
            "pair_id": "P00003",
            "prompt": "p2",
            "chosen": "gamma",
            "rejected": "delta",
            "annotator_group": "G2",
            "quality_flag": "clean",
        },
        {
            "pair_id": "P00004",
            "prompt": "p2",
            "chosen": "delta",
            "rejected": "gamma",
            "annotator_group": "G3",
            "quality_flag": "clean",
        },
        {
            "pair_id": "P00005",
            "prompt": "p3",
            "chosen": "same",
            "rejected": "same",
            "annotator_group": "G4",
            "quality_flag": "clean",
        },
    ]
    result = compute_consistency(pairs, config)
    assert result["records"]["P00001"]["conflicted"]
    assert E_GROUP_DIRECTION_CONFLICT in result["records"]["P00001"]["codes"]
    assert E_LABEL_CROSS_CONTRADICTION in result["records"]["P00001"]["codes"]
    assert E_LABEL_CROSS_CONTRADICTION in result["records"]["P00003"]["codes"]
    assert E_GROUP_DIRECTION_CONFLICT not in result["records"]["P00003"]["codes"]
    assert E_IDENTICAL_SIDE_CONTRADICTION in result["records"]["P00005"]["codes"]
    assert all(
        conflict["evidence"] for conflict in result["conflict_pairs"]
    )

