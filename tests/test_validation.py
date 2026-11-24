from matgen.errors import ErrorCode
from matgen.model import validate_material
from tests.helpers import make_config


def _raw(**overrides):
    base = {
        "material_id": "mat_x",
        "seed": 7,
        "width": 32,
        "height": 24,
        "base_color": [0.1, 0.2, 0.3],
        "roughness": 0.5,
        "normal_strength": 1.2,
        "octaves": 3,
        "frequencies": [1 / 32, 1 / 16, 1 / 8],
        "amplitudes": [0.5, 0.3, 0.2],
        "channels": ["color", "roughness", "normal"],
    }
    base.update(overrides)
    return base


def test_valid_material_passes():
    spec, error = validate_material(_raw(), make_config())
    assert error is None and spec is not None


def test_error_codes_are_distinguishable():
    config = make_config()
    cases = {
        ErrorCode.E_BASE_COLOR_RANGE: _raw(base_color=[0.0, 1.2, 0.5]),
        ErrorCode.E_ROUGHNESS_INVALID: _raw(roughness=0),
        ErrorCode.E_NORMAL_STRENGTH_INVALID: _raw(normal_strength=float("nan")),
        ErrorCode.E_OCTAVES_INVALID: _raw(octaves=0, frequencies=[1.0], amplitudes=[1.0]),
        ErrorCode.E_FREQUENCY_ORDER: _raw(octaves=2, frequencies=[0.5, 0.5], amplitudes=[1.0, 1.0]),
        ErrorCode.E_AMPLITUDE_INVALID: _raw(amplitudes=[0.5, 0.3, -0.2]),
        ErrorCode.E_CHANNEL_MISSING: _raw(channels=["color", "roughness"]),
        ErrorCode.E_DIMENSION_INVALID: _raw(width=-4),
        ErrorCode.E_GRID_DIVISIBILITY: _raw(grid_rows=5, grid_cols=5),
    }
    for expected_code, raw in cases.items():
        spec, error = validate_material(raw, config)
        assert spec is None
        assert error is not None and error.code == expected_code


def test_grid_override_divisibility():
    # 32 宽不能被 3 列整除。
    spec, error = validate_material(_raw(grid_cols=3), make_config())
    assert error is not None and error.code == ErrorCode.E_GRID_DIVISIBILITY
