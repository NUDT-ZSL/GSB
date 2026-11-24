from __future__ import annotations

import random

from bsl_workbench.parser import parse_file
from bsl_workbench.revision import compute_invalidation


def _revision_reports(sample_root, file_id: str = "revision_demo"):
    old = parse_file(
        sample_root / "revisions/v1/revision_demo.bsl", file_id=file_id
    )
    new = parse_file(
        sample_root / "revisions/v2/revision_demo.bsl", file_id=file_id
    )
    return old, new


def test_transitive_invalidation_closure(sample_root) -> None:
    old, new = _revision_reports(sample_root)
    result = compute_invalidation(old, new, file_id="revision_demo")

    assert [item["canonical_key"] for item in result["changed_segments"]] == ["/base"]
    assert result["recompute_segments"] == ["/base", "/mid", "/top"]
    by_key = {item["canonical_key"]: item for item in result["invalidated_segments"]}
    assert by_key["/top"]["path"] == ["/base", "/mid", "/top"]
    assert by_key["/top"]["distance"] == 2


def test_invalidation_is_order_independent(sample_root) -> None:
    old, new = _revision_reports(sample_root)
    baseline = compute_invalidation(old, new)

    shuffled_new = dict(new)
    rng = random.Random(356)
    shuffled_segments = list(new["segments"])
    shuffled_edges = list(new["dependencies"]["edges"])
    rng.shuffle(shuffled_segments)
    rng.shuffle(shuffled_edges)
    shuffled_new["segments"] = shuffled_segments
    shuffled_new["dependencies"] = dict(new["dependencies"])
    shuffled_new["dependencies"]["edges"] = shuffled_edges

    shuffled_old = dict(old)
    old_edges = list(old["dependencies"]["edges"])
    rng.shuffle(old_edges)
    shuffled_old["dependencies"] = dict(old["dependencies"])
    shuffled_old["dependencies"]["edges"] = old_edges

    shuffled = compute_invalidation(shuffled_old, shuffled_new)
    assert shuffled["recompute_segments"] == baseline["recompute_segments"]
    assert shuffled["invalidated_segments"] == baseline["invalidated_segments"]


def test_added_and_removed_keys_are_sorted_sources(sample_root) -> None:
    old, new = _revision_reports(sample_root)
    new["segments"] = [
        segment for segment in new["segments"] if segment["canonical_key"] != "/top"
    ]
    result = compute_invalidation(old, new)
    reasons = {
        item["canonical_key"]: item["reason"] for item in result["changed_segments"]
    }
    assert reasons["/top"] == "removed"
    assert result["changed_segments"] == sorted(
        result["changed_segments"], key=lambda item: item["canonical_key"]
    )
