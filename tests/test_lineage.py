"""Requirement 7: lineage replay and stale-result rejection."""

import pytest

from meeting_align.errors import LineageBrokenError
from meeting_align.lineage import replay_segment, verify_lineage
from meeting_align.pipeline import Pipeline


def _state_from(result):
    return result["lineage_state"]


def test_every_segment_carries_lineage(sample_data, config, tmp_path):
    pipe = Pipeline(tmp_path / "p", config)
    r = pipe.run(sample_data["batches"](), ["A", "B"])
    for seg in r["segments"]:
        lin = seg["lineage"]
        for field in ("map_cache_key", "calibration_version",
                      "seam_set_version", "frame_hop", "pcm_hash"):
            assert field in lin
        verify_lineage(seg, _state_from(r))  # must not raise


def test_replay_reproduces_timeline(sample_data, config, tmp_path):
    pipe = Pipeline(tmp_path / "p", config)
    batches = sample_data["batches"]()
    r = pipe.run(batches, ["A", "B"])
    sr = config.sample_rate
    seams_ms = [c["sample_index"] / sr * 1000.0
                for c in r["seams"]["global"]["candidates"]
                if c["kind"] == "splice"]
    gaps_ms = [[g[0] / sr * 1000.0, g[1] / sr * 1000.0]
               for g in r["seams"]["global"]["gaps"]]
    originals = [s for b in batches.values() for s in b.segments]
    for seg in r["segments"]:
        replay_segment(seg, originals, r["calibration"], seams_ms, gaps_ms,
                       config)


def test_stale_dependency_rejected(sample_data, config, tmp_path):
    pipe = Pipeline(tmp_path / "p", config)
    r = pipe.run(sample_data["batches"](), ["A", "B"])
    seg = r["segments"][0]
    state = _state_from(r)
    state["calibration_versions"][seg["batch_id"]] += 1  # dependency moved on
    with pytest.raises(LineageBrokenError) as exc:
        verify_lineage(seg, state)
    assert exc.value.code == "E_LINEAGE_BROKEN"
    assert exc.value.context["segment_id"] == seg["id"]
    assert exc.value.context["dependency"] == "calibration_version"


def test_missing_lineage_rejected(sample_data, config, tmp_path):
    pipe = Pipeline(tmp_path / "p", config)
    r = pipe.run(sample_data["batches"](), ["A", "B"])
    seg = {k: v for k, v in r["segments"][0].items() if k != "lineage"}
    with pytest.raises(LineageBrokenError) as exc:
        verify_lineage(seg, _state_from(r))
    assert exc.value.context["dependency"] == "lineage_record"


def test_stale_result_after_pcm_swap_rejected(sample_data, config, tmp_path):
    pipe = Pipeline(tmp_path / "p", config)
    r1 = pipe.run(sample_data["batches"](), ["A", "B"])
    old_segment_a = r1["segments"][0]
    r2 = pipe.run(sample_data["batches"]("batchB_v2.pcm"), ["A", "B"],
                  previous=r1)
    state = _state_from(r2)
    # batch B results recorded against the old PCM must be rejected
    stale = [s for s in r1["segments"] if s["id"].startswith("B")][0]
    with pytest.raises(LineageBrokenError):
        verify_lineage(stale, state)
    # segments of the unchanged batch A still verify
    verify_lineage(old_segment_a, state)
