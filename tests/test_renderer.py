from __future__ import annotations

import json

import numpy as np
import pytest

from medvol import Camera, SamplingConfig, Volume, VolumeRenderError, VolumeRenderer, hash_rgba
from medvol.errors import BusinessCode
from medvol.transfer_function import demo_transfer_function


def test_two_cpu_renders_are_byte_identical_and_reports_match(
    small_volume: Volume,
    small_camera: Camera,
) -> None:
    tf = demo_transfer_function((0, 4095))
    renderer = VolumeRenderer(small_volume, small_camera, tf, SamplingConfig(base_step=0.7))
    first = renderer.render(64, 64)
    second = renderer.render(64, 64)

    assert first.rgba_linear.dtype == np.float32
    assert first.rgba_linear.shape == (64, 64, 4)
    assert first.rgba_sha256 == hash_rgba(first.rgba_linear)
    assert first.rgba_sha256 == second.rgba_sha256
    assert first.rgba_linear.tobytes(order="C") == second.rgba_linear.tobytes(order="C")
    assert first.report["error_analysis"] == second.report["error_analysis"]
    json.dumps(first.to_envelope(), allow_nan=False)


def test_adaptive_is_not_worse_than_fixed_and_samples_more(
    small_volume: Volume,
    small_camera: Camera,
) -> None:
    tf = demo_transfer_function((0, 4095))
    common = dict(base_step=0.7, max_refinement=2, gradient_threshold=0.15, error_threshold=20.0)
    fixed = VolumeRenderer(small_volume, small_camera, tf, SamplingConfig(adaptive=False, **common)).render(64, 64)
    adaptive = VolumeRenderer(small_volume, small_camera, tf, SamplingConfig(adaptive=True, **common)).render(64, 64)

    fixed_error = fixed.report["error_analysis"]["total_error"]
    adaptive_error = adaptive.report["error_analysis"]["total_error"]
    assert adaptive_error["max"] <= fixed_error["max"]
    assert adaptive_error["mean"] <= fixed_error["mean"]
    assert adaptive_error["p95"] <= fixed_error["p95"]
    assert adaptive.report["sampling"]["total_samples"] >= fixed.report["sampling"]["total_samples"]
    assert adaptive.report["sampling"]["requested_base_step_world"] == fixed.report["sampling"]["requested_base_step_world"]
    assert adaptive.report["config"]["camera"] == fixed.report["config"]["camera"]


def test_error_bound_business_code_and_pixel_ratio(
    small_volume: Volume,
    small_camera: Camera,
) -> None:
    tf = demo_transfer_function((0, 4095))
    result = VolumeRenderer(
        small_volume,
        small_camera,
        tf,
        SamplingConfig(base_step=0.7, error_threshold=0.0),
    ).render(48, 48)
    assert result.business_code == BusinessCode.E_ERROR_BOUND
    report = result.report["error_analysis"]
    assert report["threshold_exceeded"] is True
    assert 0.0 < report["exceeded_pixel_ratio"] <= 1.0
    assert report["exceeded_pixel_count"] == round(report["exceeded_pixel_ratio"] * 48 * 48)
    assert result.to_envelope()["business_code"] == "E_ERROR_BOUND"


def test_renderer_rejects_step_below_tenth_voxel(small_volume: Volume, small_camera: Camera) -> None:
    with pytest.raises(VolumeRenderError) as info:
        VolumeRenderer(
            small_volume,
            small_camera,
            demo_transfer_function((0, 4095)),
            SamplingConfig(base_step=0.05),
        )
    assert info.value.code == BusinessCode.E_STEP_TOO_SMALL


def test_srgb_option_converts_without_changing_linear_buffer(
    small_volume: Volume,
    small_camera: Camera,
) -> None:
    tf = demo_transfer_function((0, 4095))
    linear = VolumeRenderer(small_volume, small_camera, tf, SamplingConfig(base_step=0.7)).render(32, 32)
    srgb = VolumeRenderer(
        small_volume,
        small_camera,
        tf,
        SamplingConfig(base_step=0.7, srgb=True),
    ).render(32, 32)
    assert linear.rgba_sha256 == srgb.rgba_sha256
    assert linear.rgba_srgb_sha256 == srgb.rgba_srgb_sha256
    assert np.array_equal(linear.rgba_linear, srgb.rgba_linear)
    assert np.any(linear.rgba_srgb != linear.rgba_linear)
    assert srgb.rgba.dtype == np.float32


def test_early_termination_reduces_samples(small_volume: Volume, small_camera: Camera) -> None:
    tf = demo_transfer_function((0, 4095))
    full = VolumeRenderer(small_volume, small_camera, tf, SamplingConfig(base_step=0.7)).render(32, 32)
    early = VolumeRenderer(
        small_volume,
        small_camera,
        tf,
        SamplingConfig(base_step=0.7, early_termination_threshold=0.02),
    ).render(32, 32)
    assert early.report["sampling"]["total_samples"] < full.report["sampling"]["total_samples"]


def test_render_safe_returns_structured_error(small_volume: Volume, small_camera: Camera) -> None:
    with pytest.raises(VolumeRenderError):
        VolumeRenderer(
            small_volume,
            small_camera,
            demo_transfer_function((0, 4095)),
            SamplingConfig(base_step=0.05),
        )
