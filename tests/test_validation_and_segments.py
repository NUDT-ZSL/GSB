from __future__ import annotations

from acoustic_alignment.engine import validate_and_prepare
from acoustic_alignment.task_model import load_task, ms_to_samples


def test_business_validation_codes_are_distinct(sample_task_path):
    task = load_task(sample_task_path)
    outcomes = {}
    for index, track in enumerate(task.tracks):
        error, prepared = validate_and_prepare(track, index, task)
        outcomes[track.track_id] = error if error is not None else prepared

    assert "E_RATE" in outcomes["bad_rate"].business_codes
    assert "E_BITS" in outcomes["bad_bits"].business_codes
    assert "E_CHANNELS" in outcomes["bad_channels"].business_codes
    assert "E_FILE" in outcomes["missing_file"].business_codes
    assert "E_START" in outcomes["bad_start"].business_codes


def test_sample_boundaries_are_exact_and_partition_frames(sample_task_path):
    task = load_task(sample_task_path)
    expected = {
        "ref_main": (48000, 14400, 72000, 96000),
        "cand_44100": (44100, 13230, 66150, 88200),
        "cand_16k": (16000, 4800, 24000, 32000),
    }
    for track_id, (rate, start, end, frames) in expected.items():
        track = task.track_by_id(track_id)
        _error, prepared = validate_and_prepare(track, 0, task)
        assert prepared is not None
        header = prepared.wav.header
        assert (header.sample_rate, header.frames) == (rate, frames)
        assert prepared.sweep_start_sample == ms_to_samples(300, rate) == start
        assert prepared.sweep_end_sample == ms_to_samples(1500, rate) == end

        result_start = prepared.sweep_start_sample
        result_end = prepared.sweep_end_sample
        assert 0 < result_start < result_end < frames
        assert result_start == start
        assert result_end == end
        assert result_end + (frames - result_end) == frames


def test_ms_conversion_uses_half_up_integer_samples():
    assert ms_to_samples(1, 44100) == 44
    assert ms_to_samples(10, 44100) == 441
    assert ms_to_samples(300, 44100) == 13230
