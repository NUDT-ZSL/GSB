"""Acceptance tests: determinism, invalidation, replay, business codes."""

from __future__ import annotations

import json
import os
import threading

import numpy as np
import pytest

from aed.cache import ContentCache, SnapshotStore, canonical_json, make_key
from aed.config import PipelineConfig
from aed.detect import detect_candidates, validate_event_types
from aed.errors import AEDError, Code
from aed.features import extract_features
from aed.pipeline import Dialog, Pipeline
from aed.postprocess import merge_candidates, validate_segments
from aed.studio import Studio
from aed.synth import synth_dialog
from aed.trace import check_stale, replay

WS = "workspace_test"


@pytest.fixture()
def studio(tmp_path):
    st = Studio(os.path.join(str(tmp_path), "ws"))
    st.generate_dataset()
    return st


@pytest.fixture()
def dialog(studio):
    return studio.load_dialog("dlg-001")


# ---- 1. feature extraction determinism & frame mismatch -------------------
def test_features_byte_identical(dialog):
    cfg = PipelineConfig()
    f1 = extract_features(dialog.pcm, cfg, dialog.dialog_id,
                          dialog.duration_ms)
    f2 = extract_features(dialog.pcm, cfg, dialog.dialog_id,
                          dialog.duration_ms)
    assert f1.tobytes() == f2.tobytes()
    assert f1.dtype == np.float64 and f1.shape[1] == 4


def test_frame_match_mismatch_code(dialog):
    with pytest.raises(AEDError) as exc:
        extract_features(dialog.pcm, PipelineConfig(), dialog.dialog_id,
                         dialog.duration_ms + 1000)
    assert exc.value.code == Code.E_FRAME_MISMATCH
    assert exc.value.details["dialog_id"] == "dlg-001"


# ---- 2. candidate detection ------------------------------------------------
def test_candidates_deterministic_and_sorted(dialog):
    cfg = PipelineConfig()
    feats = extract_features(dialog.pcm, cfg, "d", dialog.duration_ms)
    c1 = detect_candidates(feats, cfg)
    c2 = detect_candidates(feats, cfg)
    assert canonical_json(c1) == canonical_json(c2)
    keys = [(c["start_frame"], c["event_type"]) for c in c1]
    assert keys == sorted(keys, key=lambda k: k[0])


def test_event_type_invalid():
    with pytest.raises(AEDError) as exc:
        validate_event_types([{"event_type": "doorbell"}])
    assert exc.value.code == Code.E_EVENT_TYPE_INVALID


# ---- 3. post-processing ------------------------------------------------------
def test_segments_sorted_nonoverlap_rebuildable(dialog):
    cfg = PipelineConfig()
    feats = extract_features(dialog.pcm, cfg, "d", dialog.duration_ms)
    cands = detect_candidates(feats, cfg)
    s1 = merge_candidates(cands, cfg, dialog.duration_ms)
    s2 = merge_candidates(cands, cfg, dialog.duration_ms)  # independent rebuild
    assert canonical_json(s1) == canonical_json(s2)
    for a, b in zip(s1, s1[1:]):
        assert a["end_ms"] <= b["start_ms"]
    for s in s1:
        assert 0 <= s["start_ms"] < s["end_ms"] <= dialog.duration_ms


def test_overlap_and_out_of_range_codes():
    with pytest.raises(AEDError) as exc:
        validate_segments([{"start_ms": 0, "end_ms": 100, "event_seq": 0},
                           {"start_ms": 50, "end_ms": 200, "event_seq": 1}],
                          1000)
    assert exc.value.code == Code.E_OVERLAP
    with pytest.raises(AEDError) as exc:
        validate_segments([{"start_ms": 0, "end_ms": 5000, "event_seq": 0}],
                          1000)
    assert exc.value.code == Code.E_OUT_OF_RANGE


# ---- 4. content-addressed cache ----------------------------------------------
def test_cache_key_reuse_and_change(studio, dialog):
    pipeline = Pipeline(studio.cfg, studio.cache)
    r1 = pipeline.run(dialog)
    assert all(s == "recomputed" for s in r1["status"].values())
    r2 = pipeline.run(dialog)
    assert all(s == "hit" for s in r2["status"].values())
    assert r1["keys"] == r2["keys"]
    # key composition queryable by node name
    metas = studio.describe_cache("features")
    assert metas and "audio_chunks" in metas[0]["components"]
    assert "template_version" in \
        studio.describe_cache("candidates")[0]["components"]
    seg_comps = studio.describe_cache("segments")[0]["components"]
    assert {"min_event_ms", "min_gap_ms", "hop"} <= set(seg_comps)
    assert "features" in \
        studio.describe_cache("candidates")[0]["components"]["upstream"]


# ---- 5. incremental invalidation ----------------------------------------------
@pytest.mark.parametrize("change,expect_recompute,expect_reuse", [
    ({"kind": "template_upgrade", "template_version": "t2"},
     ["candidates", "segments", "metrics"], ["features"]),
    ({"kind": "truth_fix", "event_index": 0, "new_type": "cough"},
     ["metrics"], ["features", "candidates", "segments"]),
    ({"kind": "hop_change", "hop": 80},
     ["features", "candidates", "segments", "metrics"], []),
    ({"kind": "min_duration_change", "value": 100},
     ["segments", "metrics"], ["features", "candidates"]),
])
def test_incremental_matches_full_recompute(studio, change,
                                            expect_recompute, expect_reuse):
    studio.run_all()
    result = studio.apply_change(change, "dlg-001")
    assert result["recompute_nodes"] == expect_recompute
    assert result["reused_nodes"] == expect_reuse
    inc = Studio._report_bytes(result["report"])
    ref = studio.full_recompute_bytes("dlg-001", dialog=result["dialog"])
    assert inc == ref  # byte-identical with a pristine full recompute


def test_invalidation_intervals_reported(studio):
    studio.run_all()
    res = studio.apply_change({"kind": "truth_fix", "event_index": 0},
                              "dlg-001")
    assert res["affected_intervals_ms"][0][0] >= 0
    assert res["affected_events"] == [0]


# ---- 6. traceability & stale rejection -----------------------------------------
def test_replay_byte_consistent(studio):
    studio.run_all()
    records = studio.trace.load()
    assert records
    for i in range(len(records)):
        rec = records[i]
        replayed = studio.replay_event(i)
        assert replayed["event_type"] == rec["event_type"]
        assert replayed["start_ms"] == rec["start_ms"]
        assert replayed["end_ms"] == rec["end_ms"]
        assert replayed["confidence"] == rec["confidence"]
        assert rec["node_path"] == ["features", "candidates", "segments"]


def test_stale_result_rejected(studio):
    studio.run_all()
    rec = studio.trace.load()[0]
    studio.apply_change({"kind": "template_upgrade", "template_version": "t2"},
                        "dlg-001")
    with pytest.raises(AEDError) as exc:
        check_stale(rec, studio.cfg)
    assert exc.value.code == Code.E_STALE_RESULT
    assert exc.value.details["event_seq"] == rec["event_seq"]
    assert exc.value.details["old_versions"]["template_version"] == "t1"


# ---- 7. snapshot versioning & concurrency ---------------------------------------
def test_snapshot_rollback_and_replace(tmp_path):
    snaps = SnapshotStore(str(tmp_path))
    s1 = snaps.commit("lib", {"v": "t1"})
    s2 = snaps.commit("lib", {"v": "t2"})
    assert s2["version"] == s1["version"] + 1
    assert snaps.read("lib")["payload"] == {"v": "t2"}  # whole replacement
    with pytest.raises(AEDError) as exc:
        snaps.commit("lib", {"v": "t0"}, expected_version=s1["version"] - 1)
    assert exc.value.code == Code.E_SNAPSHOT_ROLLBACK


def test_snapshot_corruption_detected(tmp_path):
    snaps = SnapshotStore(str(tmp_path))
    snaps.commit("lib", {"v": "t1"})
    path = os.path.join(str(tmp_path), "lib.snap.json")
    with open(path) as fh:
        snap = json.load(fh)
    snap["payload"]["v"] = "tampered"
    with open(path, "w") as fh:
        json.dump(snap, fh)
    with pytest.raises(AEDError) as exc:
        snaps.read("lib")
    assert exc.value.code == Code.E_CACHE_CORRUPT


def test_cache_corruption_detected(tmp_path):
    cache = ContentCache(str(tmp_path))
    key = cache.put("features", {"a": 1}, b"payload")
    with open(os.path.join(str(tmp_path), f"{key}.bin"), "wb") as fh:
        fh.write(b"tampered")
    with pytest.raises(AEDError) as exc:
        cache.get(key)
    assert exc.value.code == Code.E_CACHE_CORRUPT


def test_concurrent_commit_busy(tmp_path):
    snaps = SnapshotStore(str(tmp_path))
    snaps.commit("lib", {"v": "t1"})
    lock = snaps._lock_for("lib")
    assert lock.acquire(blocking=False)
    try:
        errors = []

        def contender():
            try:
                snaps.commit("lib", {"v": "t2"})
            except AEDError as err:
                errors.append(err)

        t = threading.Thread(target=contender)
        t.start()
        t.join()
        assert errors and errors[0].code == Code.E_STAGE_BUSY
    finally:
        lock.release()


# ---- 8. validation chain & resource limits ---------------------------------------
def test_validation_codes(studio):
    with pytest.raises(AEDError) as exc:
        PipelineConfig(sample_rate=8000).validate()
    assert exc.value.code == Code.E_SAMPLE_RATE_INVALID
    with pytest.raises(AEDError) as exc:
        PipelineConfig(hop=640).validate()
    assert exc.value.code == Code.E_HOP_EXCEEDS_FRAME
    with pytest.raises(AEDError) as exc:
        PipelineConfig(template_version="t99").validate()
    assert exc.value.code == Code.E_TEMPLATE_VERSION_UNKNOWN
    with pytest.raises(AEDError) as exc:
        studio.generate_dataset(
            [("dup", 1, 4000, ["a"], "t1"), ("dup", 2, 4000, ["b"], "t1")])
    assert exc.value.code == Code.E_DUPLICATE_DIALOG


def test_resource_limits(studio, dialog):
    cfg = PipelineConfig(max_frames_resident=2)
    with pytest.raises(AEDError) as exc:
        extract_features(dialog.pcm, cfg, dialog.dialog_id,
                         dialog.duration_ms)
    assert exc.value.code == Code.E_RESOURCE_LIMIT
    studio.run_all()
    cfg = PipelineConfig(max_parallel_recompute=1)
    studio.cfg = cfg
    with pytest.raises(AEDError) as exc:
        studio.apply_change({"kind": "hop_change", "hop": 80}, "dlg-001")
    assert exc.value.code == Code.E_RESOURCE_LIMIT


def test_cache_entry_limit(tmp_path):
    cache = ContentCache(str(tmp_path), max_entries=3)
    for i in range(6):
        cache.put("n", {"i": i}, f"p{i}".encode())
    metas = [f for f in os.listdir(str(tmp_path)) if f.endswith(".json")]
    assert len(metas) == 3


# ---- synthesis determinism -----------------------------------------------------
def test_synthesis_deterministic():
    a = synth_dialog("d", 42, 4000, ["s1", "s2"], "t1")
    b = synth_dialog("d", 42, 4000, ["s1", "s2"], "t1")
    assert a[0].tobytes() == b[0].tobytes()
    assert canonical_json(a[2]) == canonical_json(b[2])
    assert canonical_json(a[3]) == canonical_json(b[3])
    row = a[2][0]
    assert {"frame_index", "event_type", "is_event",
            "sample_start", "sample_end"} <= set(row)
    assert {"dialog_id", "seed", "sample_rate", "duration_ms",
            "speaker_ids", "template_version"} <= set(a[3])
