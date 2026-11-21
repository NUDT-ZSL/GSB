"""Requirement 2: cross-batch timeline calibration."""

import numpy as np
import pytest

from meeting_align.calibration import calibrate_all, check_declared_rate
from meeting_align.errors import SampleRateMismatch
from meeting_align.seams import detect_seam_candidates
from meeting_align.util import canonical_json


def _calibrate(sample_data, config):
    batches = sample_data["batches"]()
    order = ["A", "B"]
    batch_seams = {b: detect_seam_candidates(batches[b].pcm, config) for b in order}
    global_pcm = np.concatenate([batches[b].pcm for b in order])
    global_seams = detect_seam_candidates(global_pcm, config)
    starts = {"A": 0, "B": int(batches["A"].pcm.size)}
    declared = {b: batches[b].declared_sample_rate for b in order}
    return calibrate_all(order, batch_seams, global_seams, starts, declared, config)


def test_calibration_recovers_declared_rate_ratio(sample_data, config):
    result = _calibrate(sample_data, config)
    cal_a, cal_b = result["calibrations"]["A"], result["calibrations"]["B"]
    assert cal_a["scale"] == pytest.approx(1.0, abs=1e-6)
    assert cal_a["offset_ms"] == pytest.approx(0.0, abs=1e-6)
    # batch B declares 15992 Hz against the applied 16000 Hz
    assert cal_b["scale"] == pytest.approx(15992 / 16000, abs=1e-3)
    assert cal_b["offset_ms"] == pytest.approx(12000.0, abs=50.0)
    assert cal_b["method"] == "least_squares"
    assert cal_b["residual_rms_ms"] < 20.0


def test_calibration_is_deterministic(sample_data, config):
    r1 = _calibrate(sample_data, config)
    r2 = _calibrate(sample_data, config)
    assert canonical_json(r1) == canonical_json(r2)


def test_anchor_pairs_and_residuals_recorded(sample_data, config):
    result = _calibrate(sample_data, config)
    cal_b = result["calibrations"]["B"]
    assert len(cal_b["anchor_pairs"]) >= 2
    assert len(cal_b["residuals_ms"]) == len(cal_b["anchor_pairs"])
    assert cal_b["declared_sample_rate"] == 15992
    assert cal_b["applied_sample_rate"] == 16000


def test_sample_rate_mismatch_rejected(config):
    with pytest.raises(SampleRateMismatch) as exc:
        check_declared_rate(8000, 16000, config.rate_tolerance, "B")
    assert exc.value.code == "E_SAMPLE_RATE_MISMATCH"
    assert exc.value.context["batch_id"] == "B"
