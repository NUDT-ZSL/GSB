from __future__ import annotations

import numpy as np
import pytest

from medvol import Camera, SamplingConfig, Volume, VolumeRenderError, checked_readback_size
from medvol.camera import validate_ray_directions
from medvol.errors import BusinessCode, error_envelope
from medvol.transfer_function import demo_transfer_function


def test_volume_shape_and_nan_errors() -> None:
    with pytest.raises(VolumeRenderError) as info:
        Volume(np.zeros((4, 4), dtype=np.uint16))
    assert info.value.code == BusinessCode.E_VOLUME_SHAPE

    bad = np.zeros((4, 4, 4), dtype=np.float32)
    bad[0, 0, 0] = np.nan
    with pytest.raises(VolumeRenderError) as info:
        Volume(bad)
    assert info.value.code == BusinessCode.E_VOLUME_SHAPE
    assert info.value.to_dict()["business_code"] == "E_VOLUME_SHAPE"


def test_zero_ray_direction_error() -> None:
    with pytest.raises(VolumeRenderError) as info:
        validate_ray_directions(np.zeros((1, 3), dtype=np.float64))
    assert info.value.code == BusinessCode.E_RAY_DIR

    with pytest.raises(VolumeRenderError) as info:
        Camera.look_at((1.0, 2.0, 3.0), (1.0, 2.0, 3.0))
    assert info.value.code == BusinessCode.E_RAY_DIR


def test_step_too_small_error() -> None:
    with pytest.raises(VolumeRenderError) as info:
        SamplingConfig(base_step=0.0)
    assert info.value.code == BusinessCode.E_STEP_TOO_SMALL


def test_buffer_size_error() -> None:
    buffer = np.zeros((8, 8, 4), dtype=np.float32)
    with pytest.raises(VolumeRenderError) as info:
        checked_readback_size(buffer, 9, 8)
    assert info.value.code == BusinessCode.E_BUFFER_SIZE
    assert info.value.details["expected_bytes"] == 9 * 8 * 4 * 4
    checked_readback_size(buffer, 8, 8)


def test_structured_error_json() -> None:
    payload = error_envelope(BusinessCode.E_TF_RANGE, "bad opacity", {"opacity": 1.5})
    assert payload["ok"] is False
    assert payload["business_code"] == "E_TF_RANGE"
    assert payload["error"]["details"]["opacity"] == 1.5


def test_gl_context_failure_is_mapped(
    monkeypatch: pytest.MonkeyPatch,
    small_volume: Volume,
    small_camera: Camera,
) -> None:
    from medvol.gl_renderer import OffscreenGLRenderer

    def fail_initialize(self: OffscreenGLRenderer) -> None:
        raise RuntimeError("forced context failure")

    monkeypatch.setattr(OffscreenGLRenderer, "_initialize", fail_initialize)
    with pytest.raises(VolumeRenderError) as info:
        OffscreenGLRenderer(
            small_volume,
            small_camera,
            demo_transfer_function((0, 4095)),
            SamplingConfig(base_step=0.5),
        )
    assert info.value.code == BusinessCode.E_GL_CONTEXT

