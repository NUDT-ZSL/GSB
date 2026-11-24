"""样本生成确定性与输入校验业务码测试."""

import json

import pytest

from castdiag.errors import (
    DiagError,
    E_CLOCK,
    E_DUP_SESSION,
    E_FRAME_ORDER,
    E_GEOMETRY,
    E_TIMELINE,
)
from castdiag.generator import generate
from castdiag.validate import load_events, load_manifest, validate_timeline


def test_generate_deterministic(tmp_path):
    a = generate(tmp_path / "a")
    b = generate(tmp_path / "b")
    assert a["events"] == b["events"] and a["screens"] == b["screens"]
    for rel in ("sessions.json", "frames.jsonl",
                "snapshots/v1/snapshot_v1.json",
                "snapshots/v2/shard_0.json", "snapshots/v2/shard_1.json",
                "snapshots/digests.json"):
        assert (tmp_path / "a" / rel).read_bytes() == (tmp_path / "b" / rel).read_bytes()


def test_duplicate_session_rejected(tmp_path):
    manifest = {
        "sessions": [
            {"session_id": "s1", "start_ms": 0,
             "geometry": {"width_px": 1, "height_px": 1, "scale": 1.0}},
            {"session_id": "s1", "start_ms": 0,
             "geometry": {"width_px": 1, "height_px": 1, "scale": 1.0}},
        ]
    }
    path = tmp_path / "sessions.json"
    path.write_text(json.dumps(manifest))
    with pytest.raises(DiagError) as exc:
        load_manifest(path)
    assert exc.value.code == E_DUP_SESSION


@pytest.mark.parametrize("value", [0, -1, float("inf"), float("nan"), "x"])
def test_bad_geometry_rejected(tmp_path, value):
    manifest = {
        "sessions": [
            {"session_id": "s1", "start_ms": 0,
             "geometry": {"width_px": value, "height_px": 1, "scale": 1.0}},
        ]
    }
    path = tmp_path / "sessions.json"
    path.write_text(json.dumps(manifest))
    with pytest.raises(DiagError) as exc:
        load_manifest(path)
    assert exc.value.code == E_GEOMETRY


def test_non_monotonic_frame_rejected(tmp_path):
    events = [
        {"seq": 0, "session_id": "s", "screen_id": "tv", "generation": 2,
         "frame_no": 2, "sample_ms": 33},
        {"seq": 1, "session_id": "s", "screen_id": "tv", "generation": 2,
         "frame_no": 1, "sample_ms": 66},
    ]
    path = tmp_path / "frames.jsonl"
    path.write_text("\n".join(json.dumps(e) for e in events))
    with pytest.raises(DiagError) as exc:
        load_events(path)
    assert exc.value.code == E_FRAME_ORDER


def test_negative_clock_rejected(tmp_path):
    events = [{"seq": 0, "session_id": "s", "screen_id": "tv", "generation": 2,
               "frame_no": 1, "sample_ms": -5}]
    path = tmp_path / "frames.jsonl"
    path.write_text("\n".join(json.dumps(e) for e in events))
    with pytest.raises(DiagError) as exc:
        load_events(path)
    assert exc.value.code == E_CLOCK


def test_timeline_not_convertible():
    v1 = {"base_epoch_ms": 0, "screens": {"tv": {"rel_start_s": 1.0004}}}
    with pytest.raises(DiagError) as exc:
        validate_timeline(v1, [])
    assert exc.value.code == E_TIMELINE


def test_timeline_out_of_range():
    v1 = {"base_epoch_ms": 0, "screens": {"tv": {"rel_start_s": 99.0}}}
    v2_rows = [{"ts_ms": 3000, "tolerance_ms": 500}]
    with pytest.raises(DiagError) as exc:
        validate_timeline(v1, v2_rows)
    assert exc.value.code == E_TIMELINE


def test_generated_samples_pass_validation(sample_dir):
    manifest = load_manifest(sample_dir / "sessions.json")
    events = load_events(sample_dir / "frames.jsonl")
    assert len(manifest["sessions"]) == 2
    assert len(events) == 203
