from __future__ import annotations

import numpy as np
import pytest

from medvol import (
    BusinessCode,
    Camera,
    OffscreenGLRenderer,
    SamplingConfig,
    Volume,
    VolumeRenderError,
)
from medvol.transfer_function import demo_transfer_function


def _new_renderer(
    volume: Volume,
    camera: Camera,
    config: SamplingConfig,
    size: int,
) -> OffscreenGLRenderer:
    try:
        return OffscreenGLRenderer(
            volume,
            camera,
            demo_transfer_function((0, 4095)),
            config,
            width=size,
            height=size,
        )
    except VolumeRenderError as exc:
        if exc.code == BusinessCode.E_GL_CONTEXT:
            pytest.skip(f"OpenGL 3.3 context unavailable: {exc.message}")
        raise


def test_offscreen_fbo_render_is_deterministic_and_resources_close(
    small_volume: Volume,
    small_camera: Camera,
) -> None:
    renderer = _new_renderer(small_volume, small_camera, SamplingConfig(base_step=0.7), 32)
    try:
        first = renderer.render()
        second = renderer.render()
        assert first.rgba_linear.shape == (32, 32, 4)
        assert first.rgba_linear.dtype == np.float32
        assert first.rgba_linear_sha256 == second.rgba_linear_sha256
        assert first.rgba_linear.tobytes(order="C") == second.rgba_linear.tobytes(order="C")
        assert first.report["opengl"]["readback_bytes"] == 32 * 32 * 4 * 4
        assert first.report["opengl"]["fbo_complete"] is True
        assert renderer.color_texture_id > 0
        assert renderer.live_object_count == 6
    finally:
        renderer.close()
    assert renderer.live_object_count == 0


def test_gl_adaptive_path_runs(small_volume: Volume, small_camera: Camera) -> None:
    renderer = _new_renderer(
        small_volume,
        small_camera,
        SamplingConfig(base_step=0.7, adaptive=True, max_refinement=2, gradient_threshold=0.15),
        24,
    )
    try:
        result = renderer.render()
        assert np.any(result.rgba_linear[..., 3] > 0.0)
    finally:
        renderer.close()


def test_create_destroy_loop_releases_tracked_objects(small_volume: Volume, small_camera: Camera) -> None:
    for _ in range(3):
        renderer = _new_renderer(small_volume, small_camera, SamplingConfig(base_step=0.9), 16)
        renderer.render()
        renderer.close()
        assert renderer.live_object_count == 0

