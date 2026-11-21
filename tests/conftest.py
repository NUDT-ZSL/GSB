from __future__ import annotations

import pytest

from motion_temporal.generator import ClipSpec, generate_clip


def make_spec(
    *,
    clip_id: str = "clip_test",
    frame_count: int = 10,
    motion_mode: str = "uniform",
    temporal_policy: str = "adjacent",
    object_count: int = 3,
    seed: int = 99,
) -> ClipSpec:
    return ClipSpec(
        clip_id=clip_id,
        frame_count=frame_count,
        frame_interval_ms=100,
        object_count=object_count,
        motion_mode=motion_mode,
        seed=seed,
        temporal_policy=temporal_policy,
        model_revision="rev-1",
    )


@pytest.fixture
def spec() -> ClipSpec:
    return make_spec()


@pytest.fixture
def clip(spec: ClipSpec):
    return generate_clip(spec)

