from __future__ import annotations

from dataclasses import replace

import pytest

from motion_temporal.canonical import canonical_bytes
from motion_temporal.codes import E_TIME_ORDER, E_TIME_SPAN
from motion_temporal.errors import BusinessError
from motion_temporal.sampling import expand_timeline, sample_frame_pairs, sampling_payload


def test_sampling_is_byte_deterministic_and_pairs_are_sorted(spec, clip):
    first = sampling_payload(spec, clip.frames)
    second = sampling_payload(spec, list(clip.frames))
    assert canonical_bytes(first) == canonical_bytes(second)
    assert first["timeline_checksum"] == second["timeline_checksum"]
    assert first["frame_pairs_checksum"] == second["frame_pairs_checksum"]
    pairs = first["frame_pairs"]
    keys = [
        (item["dst_frame_index"], item["interval_score"], item["delta_ms"], item["src_frame_index"])
        for item in pairs
    ]
    assert keys == sorted(keys)
    assert {item["dst_frame_index"] for item in pairs} >= set(range(1, spec.frame_count))


def test_timestamp_inversion_returns_time_order(spec, clip):
    frames = list(clip.frames)
    frames[4] = replace(frames[4], timestamp_ms=frames[3].timestamp_ms - 1)
    with pytest.raises(BusinessError) as caught:
        expand_timeline(spec, frames)
    assert caught.value.code == E_TIME_ORDER
    assert caught.value.clip_id == spec.clip_id
    assert caught.value.frame_index == 4


def test_timestamp_interval_mismatch_returns_time_span(spec, clip):
    frames = list(clip.frames)
    frames[6] = replace(frames[6], timestamp_ms=frames[6].timestamp_ms + 17)
    with pytest.raises(BusinessError) as caught:
        expand_timeline(spec, frames)
    assert caught.value.code == E_TIME_SPAN
    assert caught.value.frame_index == 6


def test_missing_frame_count_returns_time_span(spec, clip):
    with pytest.raises(BusinessError) as caught:
        expand_timeline(spec, clip.frames[:-1])
    assert caught.value.code == E_TIME_SPAN
    assert caught.value.frame_index == spec.frame_count - 1


def test_all_policies_keep_adjacent_primary_support(spec, clip):
    for policy in ("adjacent", "stride2", "pyramid", "adaptive"):
        value = replace(spec, temporal_policy=policy)
        pairs = sample_frame_pairs(value, expand_timeline(value, clip.frames))
        destinations = {item["dst_frame_index"] for item in pairs if item["stride"] == 1}
        assert destinations == set(range(1, value.frame_count))

