from __future__ import annotations

import json

import numpy as np
import pytest

from medvol.errors import BusinessCode, VolumeRenderError
from medvol.transfer_function import TransferFunction, demo_transfer_function


def test_json_round_trip_is_canonical_and_lut_is_deterministic() -> None:
    original = demo_transfer_function((0.0, 4095.0))
    text = original.to_json()
    restored = TransferFunction.from_json(text)
    assert restored.to_json() == text
    assert restored.sha256() == original.sha256()

    lut_a = original.build_lut()
    lut_b = restored.build_lut()
    np.testing.assert_array_equal(lut_a.rgba, lut_b.rgba)
    np.testing.assert_array_equal(lut_a.gradient_l1, lut_b.gradient_l1)
    values = np.asarray((0.0, 600.0, 1800.0, 4095.0), dtype=np.float64)
    np.testing.assert_array_equal(lut_a.lookup(values), lut_b.lookup(values))


def test_control_values_must_increase_and_opacity_stay_in_range() -> None:
    payload = json.loads(demo_transfer_function().to_json())
    payload["control_points"][1]["value"] = payload["control_points"][0]["value"]
    with pytest.raises(VolumeRenderError) as info:
        TransferFunction.from_dict(payload)
    assert info.value.code == BusinessCode.E_TF_RANGE

    payload = json.loads(demo_transfer_function().to_json())
    payload["control_points"][2]["opacity"] = 1.5
    with pytest.raises(VolumeRenderError) as info:
        TransferFunction.from_dict(payload)
    assert info.value.code == BusinessCode.E_TF_RANGE


def test_control_values_must_lie_inside_configured_range() -> None:
    payload = json.loads(demo_transfer_function((0.0, 1023.0)).to_json())
    payload["control_points"][-1]["value"] = 2048.0
    with pytest.raises(VolumeRenderError) as info:
        TransferFunction.from_dict(payload)
    assert info.value.code == BusinessCode.E_TF_RANGE
    assert info.value.to_dict()["business_code"] == "E_TF_RANGE"


def test_lookup_uses_prebuilt_table() -> None:
    tf = demo_transfer_function()
    lut = tf.build_lut()
    assert lut.rgba.shape == (4096, 4)
    values = np.linspace(0.0, 4095.0, 17)
    rgba = lut.lookup(values)
    assert rgba.shape == (17, 4)
    assert rgba.dtype == np.float32

