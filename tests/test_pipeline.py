from __future__ import annotations

import json
from dataclasses import replace

import pytest

from motion_temporal.canonical import canonical_bytes
from motion_temporal.codes import (
    E_CHECKPOINT_CORRUPT,
    E_DECAY_INVALID,
    E_EXTRAPOLATION_OOB,
    E_INCONSISTENT,
    E_MASK_SHAPE,
    E_OBJECT_OVERFLOW,
    E_RESOURCE_LIMIT,
    E_TIME_ORDER,
    E_TIME_SPAN,
    E_WINDOW_INVALID,
)
from motion_temporal.config import PipelineConfig
from motion_temporal.generator import generate_clip
from motion_temporal.injections import InjectionSpec
from motion_temporal.pipeline import run_batch, run_clip
from motion_temporal.selfcheck import validate_pipeline


def test_normal_report_is_byte_deterministic(tmp_path):
    from tests.conftest import make_spec

    spec = make_spec(frame_count=9)
    clip = generate_clip(spec)
    first = run_clip(clip, tmp_path / "cache1")
    second = run_clip(clip, tmp_path / "cache2")
    assert first["status"] == "ok"
    assert canonical_bytes(first) == canonical_bytes(second)


def test_sampling_injection_returns_located_business_code(tmp_path):
    from tests.conftest import make_spec

    spec = make_spec(frame_count=9)
    clip = generate_clip(spec)
    report = run_clip(
        clip,
        tmp_path / "cache",
        injections=[InjectionSpec.from_token("sampling:time_order:5")],
    )
    assert report["status"] == "error"
    assert report["business_code"] == E_TIME_ORDER
    assert report["error"]["frame_index"] == 5
    assert report["injection_records"][0]["business_code"] == E_TIME_ORDER


def test_missing_and_motion_jump_produce_attribution(tmp_path):
    from tests.conftest import make_spec

    spec = make_spec(frame_count=12)
    clip = generate_clip(spec)
    report = run_clip(
        clip,
        tmp_path / "cache",
        injections=[
            InjectionSpec.from_token("sampling:missing_observation:5"),
            InjectionSpec.from_token("encoding:motion_jump:8"),
        ],
    )
    assert report["status"] == "ok"
    categories = {item["category"] for item in report["attribution"]}
    assert {"observation_missing", "motion_abrupt"}.issubset(categories)
    abrupt = [item for item in report["attribution"] if item["category"] == "motion_abrupt"]
    assert any(item["frame_index"] == 8 for item in abrupt)


def test_graceful_stop_resumes_same_predictions(tmp_path):
    from tests.conftest import make_spec

    spec = make_spec(frame_count=12)
    clip = generate_clip(spec)
    baseline = run_clip(clip, tmp_path / "baseline")
    paused = run_clip(
        clip,
        tmp_path / "resume",
        stop_at={"stage": "aggregation", "frame": 6},
    )
    assert paused["status"] == "paused"
    assert paused["business_code"] == "S_STOPPED"
    assert [item["stage"] for item in paused["checkpoints"]["checkpoints"]] == ["sampling", "encoding"]
    resumed = run_clip(clip, tmp_path / "resume")
    assert resumed["status"] == "ok"
    assert resumed["predictions_checksum"] == baseline["predictions_checksum"]
    assert {"sampling", "encoding"}.issubset(set(resumed["reuse_recompute"]["stage_reused"]))


def test_output_injection_fails_selfcheck(tmp_path):
    from tests.conftest import make_spec

    spec = make_spec(frame_count=8)
    clip = generate_clip(spec)
    report = run_clip(
        clip,
        tmp_path / "cache",
        injections=[InjectionSpec.from_token("output:drop_frame:4")],
    )
    assert report["status"] == "error"
    assert report["business_code"] == E_INCONSISTENT
    assert "frame_set_mismatch" in report["error"]["details"]["selfcheck"]["categories"]


@pytest.mark.parametrize(
    ("token", "expected_code"),
    [
        ("sampling:time_span:4", E_TIME_SPAN),
        ("encoding:mask_shape:3", E_MASK_SHAPE),
        ("encoding:object_overflow:3", E_OBJECT_OVERFLOW),
        ("aggregation:window_zero:3", E_WINDOW_INVALID),
        ("aggregation:invalid_decay:3", E_DECAY_INVALID),
        ("aggregation:extrapolation_oob:4", E_EXTRAPOLATION_OOB),
        ("output:mask_shape:4", E_MASK_SHAPE),
    ],
)
def test_controlled_injection_matrix(token, expected_code, tmp_path):
    from tests.conftest import make_spec

    spec = make_spec(frame_count=9)
    clip = generate_clip(spec)
    report = run_clip(
        clip,
        tmp_path / token.replace(":", "_"),
        injections=[InjectionSpec.from_token(token)],
    )
    assert report["status"] == "error"
    assert report["business_code"] == expected_code
    assert report["injection_records"][0]["business_code"] == expected_code


def test_checkpoint_corruption_is_reported(tmp_path):
    from tests.conftest import make_spec

    spec = make_spec(frame_count=5)
    clip = generate_clip(spec)
    first = run_clip(clip, tmp_path / "cache")
    assert first["status"] == "ok"
    path = next((tmp_path / "cache").glob("*/checkpoint_v000001_sampling.json"))
    envelope = json.loads(path.read_text())
    envelope["payload"]["timeline"][0]["timestamp_ms"] = 999999
    path.write_text(json.dumps(envelope), encoding="utf-8")
    report = run_clip(clip, tmp_path / "cache")
    assert report["status"] == "error"
    assert report["business_code"] == E_CHECKPOINT_CORRUPT


def test_resource_limits_error_and_degrade(tmp_path):
    from tests.conftest import make_spec

    spec = make_spec(frame_count=10)
    clip = generate_clip(spec)
    error = run_clip(clip, tmp_path / "error", config=PipelineConfig(max_sequence_frames=6))
    assert error["business_code"] == E_RESOURCE_LIMIT
    degraded = run_clip(
        clip,
        tmp_path / "degraded",
        config=PipelineConfig(
            max_sequence_frames=6,
            window_length=5,
            max_history_windows=3,
            resource_policy="degrade",
        ),
    )
    assert degraded["status"] == "ok"
    categories = {item["category"] for item in degraded["degradations"]}
    assert categories == {"sequence_truncated", "history_window_truncated"}
    assert len(degraded["frames"]) == 6
    batch = run_batch(
        [clip, generate_clip(replace(spec, clip_id="other", seed=100))],
        tmp_path / "batch",
        config=PipelineConfig(max_parallel_clips=1),
    )
    assert batch["business_code"] == E_RESOURCE_LIMIT


def test_selfcheck_reports_clip_frame_and_resume_partition_issues():
    result = validate_pipeline(
        {
            "sampling": {"clip_id": "a", "timeline": [{"frame_index": 0}, {"frame_index": 1}]},
            "encoding": {"clip_id": "b", "frames": [{"frame_index": 0}]},
        },
        clip_id="a",
        expected_frames=[0, 1],
        affected_frames=[0, 1, 2],
        reused_frames=[0],
        recomputed_frames=[0, 2],
    )
    assert result["ok"] is False
    assert {
        "clip_id_mismatch",
        "frame_set_mismatch",
        "reuse_recompute_overlap",
        "reuse_recompute_out_of_range",
    }.issubset(set(result["categories"]))

