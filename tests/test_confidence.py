"""Requirement 4: confidence interval propagation."""

import pytest

from meeting_align.confidence import propagate_word_intervals
from meeting_align.errors import IntervalInvalidError

CAL = {"residual_rms_ms": 2.0}


def _mapped(shift_ms=0.0, handling="none", offsets=(0.0, 500.0)):
    return {
        "id": "S1",
        "batch_id": "A",
        "start_ms": 1100.0,
        "end_ms": 2100.0,
        "local_duration_ms": 1000.0,
        "handling": handling,
        "shift_ms": shift_ms,
        "words": [
            {"text": f"w{i}", "offset_ms": o, "global_ms": 1100.0 + o,
             "confidence": 0.8}
            for i, o in enumerate(offsets)
        ],
    }


def test_interval_model_and_attribution_recorded(config):
    out = propagate_word_intervals(_mapped(), CAL, config, config.frame_hop)
    assert out["interval_model"]["formula"]
    assert out["interval_model"]["confidence_decay_per_ms"] == \
        config.confidence_decay_per_ms
    assert out["dominant_source"] == "seam_localization"
    assert out["amplification_ratio"] >= 1.0


def test_shift_widens_interval_and_dominates(config):
    base = propagate_word_intervals(_mapped(), CAL, config, config.frame_hop)
    shifted = propagate_word_intervals(
        _mapped(shift_ms=300.0, handling="shifted"), CAL, config,
        config.frame_hop,
    )
    assert shifted["dominant_source"] == "segment_shift"
    assert shifted["time_error_ms"] > base["time_error_ms"]
    w_base = base["words"][0]["interval"]
    w_shift = shifted["words"][0]["interval"]
    assert (w_shift[1] - w_shift[0]) > (w_base[1] - w_base[0])


def test_calibration_residual_enters_interval(config):
    out = propagate_word_intervals(
        _mapped(), {"residual_rms_ms": 100.0}, config, config.frame_hop
    )
    assert out["dominant_source"] == "batch_calibration"
    assert out["interval_model"]["terms_ms"]["batch_calibration"] == 100.0


def test_word_offset_out_of_segment_rejected(config):
    bad = _mapped(offsets=(0.0, 5000.0))
    with pytest.raises(IntervalInvalidError) as exc:
        propagate_word_intervals(bad, CAL, config, config.frame_hop)
    assert exc.value.code == "E_INTERVAL_INVALID"
    assert exc.value.context["segment_id"] == "S1"


def test_negative_word_offset_rejected(config):
    bad = _mapped(offsets=(-5.0,))
    with pytest.raises(IntervalInvalidError):
        propagate_word_intervals(bad, CAL, config, config.frame_hop)
