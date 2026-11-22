from __future__ import annotations

import pytest

from flightfusion.errors import BusinessError, ErrorCode
from flightfusion.formats import (
    detect_format,
    parse_description_file,
    parse_document,
)


def test_three_formats_normalize_same_sensor_fields(workspace_dir):
    base = workspace_dir / "data" / "formats"
    v1, d1 = parse_description_file(base / "sensors.v1.txt")
    v2, d2 = parse_description_file(base / "sensors.v2.csv")
    v3, d3 = parse_description_file(base / "sensors.v3.json")
    assert (v1, v2, v3) == (1, 2, 3)
    for sensor_id in ("gyro", "accel", "mag"):
        assert set(d1) == set(d2) == set(d3)
        assert d1[sensor_id].sensor_type == d2[sensor_id].sensor_type
        assert d1[sensor_id].range_limit == d2[sensor_id].range_limit
        assert d1[sensor_id].sample_rate == d2[sensor_id].sample_rate
        assert d1[sensor_id].axis_order == d2[sensor_id].axis_order == d3[sensor_id].axis_order
        assert d1[sensor_id].calibration_version == d2[sensor_id].calibration_version
        assert d1[sensor_id].bias == d2[sensor_id].bias
        assert d1[sensor_id].scale == d2[sensor_id].scale
        assert d3[sensor_id].axis_coupling != d1[sensor_id].axis_coupling
        assert "noise_model" in d3[sensor_id].present_fields


def test_dispatch_without_explicit_header(workspace_dir):
    v1_text = (workspace_dir / "data" / "formats" / "sensors.v1.txt").read_text()
    v2_text = (workspace_dir / "data" / "formats" / "sensors.v2.csv").read_text()
    assert detect_format(v1_text)[0] == 1
    assert detect_format(v2_text)[0] == 2
    _, descriptions = parse_document(v1_text.replace("# FORMAT=v1\n", ""))
    assert len(descriptions) == 3


@pytest.mark.parametrize(
    ("filename", "code", "field", "line"),
    [
        ("unknown_version.v1.txt", ErrorCode.VERSION_UNSUPPORTED, "format_version", 1),
        ("field_count.v1.txt", ErrorCode.FORMAT_CORRUPT, "row", 2),
        ("missing_sensor_id.v2.csv", ErrorCode.FORMAT_CORRUPT, "sensor_id", 3),
        ("bad_number.v2.csv", ErrorCode.FORMAT_CORRUPT, "sample_rate", 3),
        ("axis_missing.v1.txt", ErrorCode.AXIS_MAPPING_INVALID, "axis_order", 2),
        ("range_out_of_bounds.v1.txt", ErrorCode.FORMAT_CORRUPT, "range", 2),
        ("corrupt.v3.json", ErrorCode.FORMAT_CORRUPT, "$", 1),
    ],
)
def test_invalid_samples_have_business_code_field_and_line(
    workspace_dir, filename, code, field, line
):
    path = workspace_dir / "data" / "invalid" / filename
    with pytest.raises(BusinessError) as caught:
        parse_description_file(path)
    error = caught.value
    assert error.code == code
    assert error.field == field
    assert error.line == line
    assert error.stage == "parse"


def test_v3_missing_required_field_is_structured_error():
    text = """
    {"version": 3, "sensors": [
      {"sensor_id": "gyro", "range": 2000, "axis_order": ["x","y","z"],
       "calibration": {"version": 3}}
    ]}
    """
    with pytest.raises(BusinessError) as caught:
        parse_document(text)
    assert caught.value.code == ErrorCode.FORMAT_CORRUPT
    assert caught.value.field == "sensors[0].sample_rate"


def test_unknown_json_version_is_unsupported():
    with pytest.raises(BusinessError) as caught:
        parse_document('{"version": 9, "sensors": []}')
    assert caught.value.code == ErrorCode.VERSION_UNSUPPORTED
    assert caught.value.line == 1

