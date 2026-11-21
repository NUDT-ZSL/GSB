"""Requirement 6: targeted invalidation after reorder/replace/hop change."""

from meeting_align.config import AlignConfig
from meeting_align.pipeline import Pipeline
from meeting_align.util import canonical_json

STRIP_KEYS = ("segments", "confidence", "calibration", "total_duration_ms",
              "applied_hop", "hashes", "versions")


def _strip(result):
    return {k: result[k] for k in STRIP_KEYS}


def test_replace_batch_recomputes_only_that_batch(sample_data, config, tmp_path):
    pipe = Pipeline(tmp_path / "p", config)
    r1 = pipe.run(sample_data["batches"](), ["A", "B"])
    r2 = pipe.run(sample_data["batches"]("batchB_v2.pcm"), ["A", "B"], previous=r1)
    assert sorted(r2["invalidation"]["recompute"]) == ["B0", "B1", "B2", "B3"]
    assert sorted(r2["invalidation"]["reuse"]) == [
        "A0", "A1", "A2", "A3", "A4"
    ]


def test_partial_change_identical_to_full_recompute(sample_data, config,
                                                    tmp_path):
    pipe = Pipeline(tmp_path / "p", config)
    r1 = pipe.run(sample_data["batches"](), ["A", "B"])
    r2 = pipe.run(sample_data["batches"]("batchB_v2.pcm"), ["A", "B"], previous=r1)
    fresh = Pipeline(tmp_path / "fresh", config)
    fresh.run(sample_data["batches"](), ["A", "B"])
    r3 = fresh.run(sample_data["batches"]("batchB_v2.pcm"), ["A", "B"])
    assert canonical_json(_strip(r2)) == canonical_json(_strip(r3))


def test_reorder_batches_affects_all_segments(sample_data, config, tmp_path):
    pipe = Pipeline(tmp_path / "p", config)
    batches = sample_data["batches"]()
    r1 = pipe.run(batches, ["A", "B"])
    r2 = pipe.run(batches, ["B", "A"], previous=r1)
    # both batch origins move when the concatenation order flips
    assert sorted(r2["invalidation"]["recompute"]) == [
        "A0", "A1", "A2", "A3", "A4", "B0", "B1", "B2", "B3"
    ]
    assert r2["invalidation"]["reuse"] == []


def test_hop_change_invalidates_everything(sample_data, tmp_path):
    cfg1 = AlignConfig()
    pipe = Pipeline(tmp_path / "p", cfg1)
    r1 = pipe.run(sample_data["batches"](), ["A", "B"])
    cfg2 = AlignConfig(frame_hop=320)
    pipe2 = Pipeline(tmp_path / "p2", cfg2)
    # reuse the previous dependency records to simulate a hop adjustment
    r2 = pipe2.run(sample_data["batches"](), ["A", "B"], previous=r1)
    assert r2["invalidation"]["reuse"] == []
    assert len(r2["invalidation"]["recompute"]) == 9


def test_unaffected_segments_come_from_cache(sample_data, config, tmp_path):
    pipe = Pipeline(tmp_path / "p", config)
    r1 = pipe.run(sample_data["batches"](), ["A", "B"])
    key_a0 = r1["cache_keys"]["segments"]["A0"]
    assert pipe.cache.get(key_a0) is not None
    r2 = pipe.run(sample_data["batches"]("batchB_v2.pcm"), ["A", "B"], previous=r1)
    # the reused segment keeps the identical cache key and payload
    assert r2["cache_keys"]["segments"]["A0"] == key_a0
    desc = pipe.cache.describe(key_a0)
    assert desc["key_components"]["ns"] == "segment_mapping"
