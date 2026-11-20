import numpy as np
import pytest

from opchain.adjudication import compare_arrays
from opchain.errors import ErrorCode


def test_identical_arrays_pass_float64():
    array = np.linspace(-2, 2, 32, dtype=np.float64)
    report = compare_arrays(array, array.copy(), "float64", 1e-8)
    assert report["violation_count"] == 0
    assert report["max_abs_diff"] == 0.0
    assert report["tolerance"]["precision"] == "float64"


def test_violation_reports_position_and_magnitude():
    expected = np.ones((4, 5), dtype=np.float64)
    actual = expected.copy()
    actual[2, 3] += 1.0
    report = compare_arrays(actual, expected, "float64", 1e-8, limit=5)
    assert report["violation_count"] == 1
    assert report["violations"][0]["multi_index"] == [2, 3]
    assert report["violations"][0]["abs_diff"] == pytest.approx(1.0)
    assert report["tolerance"]["precision"] == "float64"


def test_layout_corruption_is_detected():
    expected = np.arange(16, dtype=np.float64).reshape(4, 4)
    actual = expected.T.copy().reshape(4, 4)
    report = compare_arrays(actual, expected, "float64", 1e-8)
    assert report["violation_count"] > 0
    assert report["violations"][0]["multi_index"]
