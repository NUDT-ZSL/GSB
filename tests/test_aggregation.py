from __future__ import annotations

import pytest

from motion_temporal.aggregation import aggregate_temporal
from motion_temporal.canonical import canonical_bytes
from motion_temporal.codes import E_DECAY_INVALID, E_WINDOW_INVALID
from motion_temporal.encoding import encode_motion
from motion_temporal.errors import BusinessError
from motion_temporal.sampling import sampling_payload


def _encoded(spec, clip):
    return encode_motion(spec, clip.frames, sampling_payload(spec, clip.frames))


def test_aggregation_is_deterministic_and_traceable(spec, clip):
    encoded = _encoded(spec, clip)
    first = aggregate_temporal(spec, encoded, window_length=3, decay=0.8)
    second = aggregate_temporal(spec, encoded, window_length=3, decay=0.8)
    assert canonical_bytes(first) == canonical_bytes(second)
    assert len(first["frames"]) == spec.frame_count
    assert first["frames"][0]["history_window"]["entry_count"] == 0
    assert first["frames"][5]["history_window"]["entry_count"] == 3
    assert all(frame["state_hash"] for frame in first["frames"])
    assert all(frame["history_window"]["digest"] for frame in first["frames"])


def test_invalid_window_and_decay(spec, clip):
    encoded = _encoded(spec, clip)
    with pytest.raises(BusinessError) as caught:
        aggregate_temporal(spec, encoded, window_length=0)
    assert caught.value.code == E_WINDOW_INVALID
    with pytest.raises(BusinessError) as caught:
        aggregate_temporal(spec, encoded, window_length=3, decay=0)
    assert caught.value.code == E_DECAY_INVALID


def test_window_length_affects_missing_frame_recursion(spec):
    from tests.conftest import make_spec

    accelerated = make_spec(motion_mode="accelerated", frame_count=10)
    from motion_temporal.generator import generate_clip

    generated = generate_clip(accelerated, missing_frames=[7, 8])
    encoded = _encoded(accelerated, generated)
    short = aggregate_temporal(accelerated, encoded, window_length=1, decay=0.9)
    long = aggregate_temporal(accelerated, encoded, window_length=4, decay=0.9)
    assert short["frames"][8]["state_hash"] != long["frames"][8]["state_hash"]
    assert short["frames"][8]["objects"][0]["box"] != long["frames"][8]["objects"][0]["box"]

