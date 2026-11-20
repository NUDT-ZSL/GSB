import copy
import json
import threading
from dataclasses import replace

import numpy as np
import pytest

from vad_pipeline import errors
from vad_pipeline.cache import ArtifactCache
from vad_pipeline.cli import main as cli_main
from vad_pipeline.codec import dumps
from vad_pipeline.config import VadConfig, validate_config
from vad_pipeline.lineage import replay_frame_decision, replay_segment
from vad_pipeline.nodes import (
    compute_features,
    compute_noise,
    compute_segments,
    replay_noise_scalar,
    validate_segments,
)
from vad_pipeline.pipeline import Pipeline, affected_nodes
from vad_pipeline.synth import synth_clip


def make_clip(clip_id="clip_t", seed=7, duration_ms=3000):
    pcm, truth = synth_clip(clip_id, seed, duration_ms)
    return {
        "clip_id": clip_id,
        "seed": seed,
        "duration_ms": duration_ms,
        "sample_rate": 16000,
        "pcm": pcm,
        "truth": truth,
    }


@pytest.fixture()
def clip():
    return make_clip()


@pytest.fixture()
def pipeline(tmp_path):
    cfg = VadConfig()
    cache = ArtifactCache(tmp_path / "cache", max_entries=cfg.max_cache_entries)
    return Pipeline(cache, cfg)


# --- requirement 1: deterministic features ---------------------------------

def test_features_deterministic_and_match_truth(clip):
    a = compute_features(clip["pcm"], 16000, 10, 10)
    b = compute_features(clip["pcm"], 16000, 10, 10)
    assert dumps(a) == dumps(b)
    assert a["n_frames"] == len(clip["truth"]) == 300
    for i, fr in enumerate(clip["truth"][:5]):
        assert fr["sample_start"] == i * 160
        assert fr["sample_end"] == (i + 1) * 160


def test_frame_count_mismatch_reports_clip_id(clip, pipeline):
    clip["truth"] = clip["truth"][:-1]
    with pytest.raises(errors.VadError) as exc:
        pipeline.run_clip(clip)
    assert exc.value.code == errors.E_FRAME_COUNT
    assert exc.value.details["clip_id"] == "clip_t"
    assert exc.value.details["expected"] == 299
    assert exc.value.details["actual"] == 300


# --- requirement 2: noise percentile estimation -----------------------------

def test_percentile_invalid():
    energy = np.linspace(0.1, 1.0, 50)
    with pytest.raises(errors.VadError) as exc:
        compute_noise(energy, list(range(50)), percentile=0.0, percentile_window=5)
    assert exc.value.code == errors.E_PERCENTILE_INVALID
    with pytest.raises(errors.VadError):
        compute_noise(energy, list(range(50)), percentile=100.5, percentile_window=5)


def test_noise_unestimable():
    energy = np.linspace(0.1, 1.0, 10)
    with pytest.raises(errors.VadError) as exc:
        compute_noise(energy, [0, 1, 2], percentile=20.0, percentile_window=25)
    assert exc.value.code == errors.E_NOISE_UNESTIMABLE
    assert exc.value.details["available"] == 3
    assert exc.value.details["required"] == 25


def test_noise_scalar_replayable_from_used_frames():
    rng = np.random.default_rng(0)
    energy = rng.random(200)
    inactive = list(range(0, 200, 2))
    art = compute_noise(energy, inactive, percentile=30.0, percentile_window=15)
    assert len(art["used_frames"]) == 15
    assert replay_noise_scalar(energy, art["used_frames"]) == art["noise"]
    again = compute_noise(energy, inactive, percentile=30.0, percentile_window=15)
    assert dumps(again) == dumps(art)


# --- requirement 3: hysteresis decision + segment merge ---------------------

def test_segments_sorted_non_overlapping_in_range(pipeline, clip):
    res = pipeline.run_clip(clip)
    prev_end = 0
    for start, end in res["segments"]:
        assert start >= prev_end
        assert 0 <= start < end <= clip["duration_ms"]
        prev_end = end
    assert res["thresholds"]["low"] == pytest.approx(res["noise"] * 2.0)
    assert res["thresholds"]["high"] == pytest.approx(res["noise"] * 4.0)


def test_segment_validation_errors():
    with pytest.raises(errors.VadError) as exc:
        validate_segments([[0, 100], [50, 200]], 1000)
    assert exc.value.code == errors.E_OVERLAP
    with pytest.raises(errors.VadError) as exc:
        validate_segments([[0, 100], [200, 5000]], 1000)
    assert exc.value.code == errors.E_OUT_OF_RANGE


def test_min_segment_length_change_drops_short_segments():
    decisions = np.zeros(100, dtype=np.bool_)
    decisions[10:14] = True
    decisions[50:90] = True
    short = compute_segments(decisions, 10, 10, 1000, 60, 40)["segments"]
    assert short == [[500, 900]]
    kept = compute_segments(decisions, 10, 10, 1000, 30, 40)["segments"]
    assert kept == [[100, 140], [500, 900]]


# --- requirement 4: content-addressed cache ---------------------------------

def test_cache_key_reuse_and_recompute(pipeline, clip):
    first = pipeline.run_clip(clip)
    assert first["recomputed"] == ["features", "noise", "decision", "segments"]
    second = pipeline.run_clip(clip)
    assert second["reused"] == ["features", "noise", "decision", "segments"]
    assert second["recomputed"] == []
    assert dumps(second["_artifacts"]["features"]) == dumps(
        first["_artifacts"]["features"]
    )


def test_cache_key_components_exportable(pipeline, clip):
    res = pipeline.run_clip(clip)
    comps = res["lineage"]["components"]
    assert comps["features"]["pcm_sha256"]
    assert comps["features"]["hop_ms"] == 10
    assert comps["noise"]["features"] and comps["noise"]["percentile"] == 20.0
    assert comps["decision"]["noise"] and comps["decision"]["mult_high"] == 4.0
    assert comps["segments"]["decision"] and comps["segments"]["min_speech_ms"] == 60
    keys = res["lineage"]["keys"]
    assert len(set(keys.values())) == 4


# --- requirement 5: directed invalidation -----------------------------------

def test_affected_nodes_mapping():
    base = VadConfig()
    assert affected_nodes(base, replace(base, mult_high=5.0)) == ["decision", "segments"]
    assert affected_nodes(base, replace(base, min_speech_ms=90)) == ["segments"]
    assert affected_nodes(base, replace(base, percentile=35.0)) == [
        "noise",
        "decision",
        "segments",
    ]
    assert affected_nodes(base, replace(base, hop_ms=5)) == [
        "features",
        "noise",
        "decision",
        "segments",
    ]


@pytest.mark.parametrize(
    "field,value,expected_recomputed",
    [
        ("percentile", 35.0, ["noise", "decision", "segments"]),
        ("mult_high", 5.0, ["decision", "segments"]),
        ("min_speech_ms", 90, ["segments"]),
    ],
)
def test_recalibration_directed_and_byte_identical(
    pipeline, clip, field, value, expected_recomputed
):
    old = pipeline.run_clip(clip)
    new_cfg = replace(pipeline.cfg, **{field: value})
    report = pipeline.recalibrate(clip, new_cfg, old)
    assert report["recomputed"] == expected_recomputed
    assert report["reused"] == [
        n
        for n in ["features", "noise", "decision", "segments"]
        if n not in expected_recomputed
    ]
    assert report["byte_identical_to_full_recompute"] is True
    assert report["selfcheck"] == {"no_overlap": True, "covers_affected": True}


# --- requirement 6: lineage replay ------------------------------------------

def test_lineage_replay_roundtrip(pipeline, clip):
    cache = pipeline.cache
    res = pipeline.run_clip(clip)
    lineage = res["lineage"]
    for frame_index in (0, 100, res["n_frames"] - 1):
        assert replay_frame_decision(cache, lineage, frame_index) == bool(
            res["decisions"][frame_index]
        )
    for i, seg in enumerate(res["segments"]):
        assert replay_segment(cache, lineage, i) == list(map(int, seg))


def test_lineage_broken_reports_node_and_field(pipeline, clip):
    cache = pipeline.cache
    res = pipeline.run_clip(clip)
    bad = copy.deepcopy(res["lineage"])
    bad["components"]["decision"]["mult_high"] = 999.0
    with pytest.raises(errors.VadError) as exc:
        replay_frame_decision(cache, bad, 0)
    assert exc.value.code == errors.E_LINEAGE_BROKEN
    assert exc.value.details["node"] == "decision"
    assert exc.value.details["field"] in ("high_threshold", "decisions[0]")


def test_lineage_broken_missing_artifact(pipeline, clip):
    cache = pipeline.cache
    res = pipeline.run_clip(clip)
    bad = copy.deepcopy(res["lineage"])
    bad["keys"]["noise"] = "0" * 64
    with pytest.raises(errors.VadError) as exc:
        replay_frame_decision(cache, bad, 0)
    assert exc.value.code == errors.E_LINEAGE_BROKEN
    assert exc.value.details["node"] == "noise"
    assert exc.value.details["field"] == "artifact"


# --- requirement 7: snapshot replacement + concurrency ----------------------

def test_snapshot_corrupt_and_checksum(tmp_path):
    cache = ArtifactCache(tmp_path / "c")
    cache.commit("k1", b"data")
    assert cache.get("k1") == b"data"
    (tmp_path / "c" / "snapshot.json").write_bytes(b"not json")
    with pytest.raises(errors.VadError) as exc:
        cache.get("k1")
    assert exc.value.code == errors.E_CACHE_CORRUPT

    cache2 = ArtifactCache(tmp_path / "c2")
    cache2.commit("k1", b"data")
    snap_path = tmp_path / "c2" / "snapshot.json"
    snap = json.loads(snap_path.read_text())
    snap["entries"]["k1"]["size"] = 999
    snap_path.write_text(json.dumps(snap))
    with pytest.raises(errors.VadError) as exc:
        cache2.get("k1")
    assert exc.value.code == errors.E_CACHE_CORRUPT


def test_snapshot_wholesale_replace_on_external_change(tmp_path):
    root = tmp_path / "c"
    cache_a = ArtifactCache(root)
    cache_a.commit("k1", b"a")
    cache_b = ArtifactCache(root)
    cache_b.commit("k2", b"b")
    assert cache_a.version == 2
    assert set(cache_a.entries()) == {"k1", "k2"}
    assert cache_a.get("k2") == b"b"


def test_concurrent_commit_single_winner(tmp_path):
    root = tmp_path / "c"
    cache = ArtifactCache(root)
    key = "deadbeef"
    (cache.locks_dir / f"{key}.lock").write_bytes(b"")
    with pytest.raises(errors.VadError) as exc:
        cache.commit(key, b"x")
    assert exc.value.code == errors.E_CACHE_BUSY

    barrier = threading.Barrier(2)
    outcomes = []

    def worker(tag):
        c = ArtifactCache(root)
        barrier.wait()
        try:
            c.commit("shared", tag.encode())
            outcomes.append("ok")
        except errors.VadError as exc:
            outcomes.append(exc.code)

    t1 = threading.Thread(target=worker, args=("a",))
    t2 = threading.Thread(target=worker, args=("b",))
    t1.start()
    t2.start()
    t1.join()
    t2.join()
    assert outcomes.count("ok") >= 1
    assert ArtifactCache(root).get("shared") in (b"a", b"b")


# --- requirement 8: validation, limits, CLI ---------------------------------

def test_config_validation_codes():
    with pytest.raises(errors.VadError) as exc:
        validate_config(replace(VadConfig(), sample_rate=8000))
    assert exc.value.code == errors.E_SAMPLE_RATE_INVALID
    with pytest.raises(errors.VadError) as exc:
        validate_config(replace(VadConfig(), frame_ms=12))
    assert exc.value.code == errors.E_FRAME_MS_INVALID
    with pytest.raises(errors.VadError) as exc:
        validate_config(replace(VadConfig(), hop_ms=20, frame_ms=10))
    assert exc.value.code == errors.E_HOP_INVALID
    with pytest.raises(errors.VadError) as exc:
        validate_config(replace(VadConfig(), mult_low=5.0, mult_high=2.0))
    assert exc.value.code == errors.E_MULTIPLIER_INVALID


def test_duplicate_clip_id(tmp_path):
    cfg = VadConfig()
    pipe = Pipeline(ArtifactCache(tmp_path / "c"), cfg)
    clips = [make_clip("dup", 1), make_clip("dup", 2)]
    with pytest.raises(errors.VadError) as exc:
        pipe.run_manifest(clips)
    assert exc.value.code == errors.E_DUPLICATE_CLIP_ID


def test_resource_limit_frames(tmp_path):
    cfg = replace(VadConfig(), max_frames_in_memory=10)
    pipe = Pipeline(ArtifactCache(tmp_path / "c"), cfg)
    with pytest.raises(errors.VadError) as exc:
        pipe.run_manifest([make_clip("a", 1), make_clip("b", 2)])
    assert exc.value.code == errors.E_RESOURCE_LIMIT


def test_cache_entry_limit_degrades(tmp_path):
    cfg = replace(VadConfig(), max_cache_entries=2)
    cache = ArtifactCache(tmp_path / "c", max_entries=2)
    pipe = Pipeline(cache, cfg)
    res = pipe.run_clip(make_clip())
    assert res["degraded"] is True
    assert len(cache.entries()) == 2


def test_cli_demo(tmp_path):
    workdir = tmp_path / "demo"
    out = tmp_path / "result.json"
    rc = cli_main(["demo", "--workdir", str(workdir), "--out", str(out)])
    assert rc == 0
    doc = json.loads(out.read_text())
    assert len(doc["clips"]) == 3
    for rec in doc["recalibrations"]:
        assert rec["byte_identical_to_full_recompute"] is True
        assert rec["selfcheck"]["no_overlap"] is True
        assert rec["selfcheck"]["covers_affected"] is True
        assert rec["recomputed"] == ["noise", "decision", "segments"]
        assert rec["reused"] == ["features"]
    assert all(c["ok"] for c in doc["lineage_checks"])
    tamper = doc["lineage_tamper_check"]
    assert tamper["observed"]["code"] == "E_LINEAGE_BROKEN"
    assert doc["business_codes"] == []


def test_cli_demo_parallel(tmp_path):
    workdir = tmp_path / "demo_par"
    out = tmp_path / "result.json"
    rc = cli_main(
        ["demo", "--workdir", str(workdir), "--jobs", "2", "--out", str(out)]
    )
    assert rc == 0
    doc = json.loads(out.read_text())
    assert len(doc["clips"]) == 3
    assert all(c["ok"] for c in doc["lineage_checks"])
