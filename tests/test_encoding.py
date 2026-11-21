from __future__ import annotations

from dataclasses import replace

import pytest

from motion_temporal.canonical import canonical_bytes
from motion_temporal.codes import E_MASK_SHAPE, E_OBJECT_OVERFLOW
from motion_temporal.encoding import encode_motion
from motion_temporal.errors import BusinessError
from motion_temporal.models import TargetObservation
from motion_temporal.sampling import sampling_payload


def test_encoding_is_byte_deterministic(spec, clip):
    sampling = sampling_payload(spec, clip.frames)
    first = encode_motion(spec, clip.frames, sampling)
    second = encode_motion(spec, list(clip.frames), sampling)
    assert canonical_bytes(first) == canonical_bytes(second)
    assert len(first["frames"]) == spec.frame_count
    assert all(len(frame["objects"]) == spec.object_count for frame in first["frames"])


def test_occluded_target_keeps_placeholder_and_no_speed(spec, clip):
    sampling = sampling_payload(spec, clip.frames)
    frames = list(clip.frames)
    target = frames[5].targets[0]
    frames[5] = replace(
        frames[5],
        targets=(
            replace(target, occluded=True, mask_coords=()),
            *frames[5].targets[1:],
        ),
    )
    encoded = encode_motion(spec, frames, sampling)
    occluded = encoded["frames"][5]["objects"][0]
    assert occluded["status"] == "occluded"
    assert occluded["velocity"] is None
    assert occluded["speed"] is None
    assert occluded["extrapolation_confidence"] == 0.0


def test_object_overflow_is_located(spec, clip):
    sampling = sampling_payload(spec, clip.frames)
    frames = list(clip.frames)
    target = frames[4].targets[0]
    frames[4] = replace(
        frames[4],
        targets=frames[4].targets
        + (
            TargetObservation(
                object_id=spec.object_count,
                box=target.box,
                mask_shape=target.mask_shape,
                mask_coords=target.mask_coords,
            ),
        ),
    )
    with pytest.raises(BusinessError) as caught:
        encode_motion(spec, frames, sampling)
    assert caught.value.code == E_OBJECT_OVERFLOW
    assert caught.value.frame_index == 4
    assert caught.value.clip_id == spec.clip_id


def test_mask_shape_mismatch_is_located(spec, clip):
    sampling = sampling_payload(spec, clip.frames)
    frames = list(clip.frames)
    target = frames[3].targets[0]
    frames[3] = replace(
        frames[3],
        targets=(replace(target, mask_shape=(5, 5)), *frames[3].targets[1:]),
    )
    with pytest.raises(BusinessError) as caught:
        encode_motion(spec, frames, sampling)
    assert caught.value.code == E_MASK_SHAPE
    assert caught.value.frame_index == 3

