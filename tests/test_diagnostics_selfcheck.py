import copy

import numpy as np
import pytest

from seg_diag.codes import CODES
from seg_diag.config_io import read_v1_config, read_v2_config
from seg_diag.diagnostics import build_error_report
from seg_diag.errors import DiagnosticError
from seg_diag.ledger import build_ledger
from seg_diag.numerics import flush_subnormal, strict_threshold, tolerant_threshold
from seg_diag.sample_generator import generate_sample_directory, load_sample_arrays
from seg_diag.selfcheck import run_self_check


@pytest.fixture()
def context(tmp_path):
    sample_dir = generate_sample_directory(tmp_path / "sample", seed=1001, force=True)
    arrays = load_sample_arrays(sample_dir)
    v1 = read_v1_config(sample_dir / "config_v1.json")
    v2 = read_v2_config(sample_dir / "config_v2")
    ledger = build_ledger(arrays, v1, v2, seed=1001)
    return arrays, v1, v2, ledger


def test_subnormal_protection_and_threshold_strategies():
    tiny = np.nextafter(0.0, 1.0)
    assert tiny > 0.0
    assert flush_subnormal(tiny) == 0.0
    assert strict_threshold(tiny, 0.0)

    accepted, band = tolerant_threshold(0.7500000000001, 0.75, tolerance=1e-9)
    assert band
    assert accepted
    accepted_low, band_low = tolerant_threshold(0.7499999999999, 0.75, tolerance=1e-9)
    assert band_low
    assert accepted_low
    rejected, in_band = tolerant_threshold(
        0.7499999999999, 0.75, tolerance=1e-9, band_decision="reject"
    )
    assert in_band and not rejected


def test_error_report_is_finite_and_locates_source(context):
    arrays, _v1, v2, ledger = context
    report = build_error_report(arrays, ledger, v2)

    assert report["normalization"]["v1"]["finite"]
    assert report["normalization"]["v2"]["finite"]
    assert report["threshold_decision"]["strict"]["strategy"] == "strict"
    assert report["threshold_decision"]["tolerant"]["tolerance_band_pixel_count"] >= 0
    assert report["dominant_error_source"] in report["source_contributions"]
    assert "error_amplification_vs_float64" in report["normalization"]
    assert "error_amplification_vs_normalization" in report["threshold_decision"]
    assert "error_amplification_vs_threshold" in report["semantic_merge"]


def test_self_check_passes_and_tampering_returns_inconsistent(context):
    arrays, v1, v2, ledger = context
    result = run_self_check(
        arrays=arrays,
        v1_config=v1,
        v2_config=v2,
        ledger=ledger,
        seed=1001,
    )
    assert result["business_code"] == CODES.OK

    tampered = copy.deepcopy(ledger)
    tampered["rows"][0]["winner"]["instance_id"] = 999
    with pytest.raises(DiagnosticError) as exc:
        run_self_check(
            arrays=arrays,
            v1_config=v1,
            v2_config=v2,
            ledger=tampered,
            seed=1001,
        )
    assert exc.value.code == CODES.INCONSISTENT
    assert exc.value.details["first_mismatch"]
    assert exc.value.details["business_code"] == CODES.INCONSISTENT
