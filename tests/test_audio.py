"""Onset detection unit tests (requirements 2, 5, 14)."""

from __future__ import annotations

import numpy as np
import pytest

from lip_sync.audio import OnsetConfig, detect_onset_ms, read_wav

from .helpers import SAMPLE_RATE, tone, write_wav


@pytest.mark.parametrize("onset_ms", [0.0, 5.0, 120.0, 300.0, 777.5])
def test_onset_matches_known_silence_prefix(onset_ms: float) -> None:
    samples = tone(duration_ms=onset_ms + 500.0, onset_ms=onset_ms)
    result = detect_onset_ms(samples, SAMPLE_RATE)
    assert not result.silent
    assert result.onset_ms is not None
    # Requirement 2: within 5 ms of the hand-labelled onset.
    assert abs(result.onset_ms - onset_ms) <= 5.0


def test_onset_is_sample_accurate_for_hard_edge() -> None:
    samples = tone(duration_ms=600.0, onset_ms=250.0)
    result = detect_onset_ms(samples, SAMPLE_RATE)
    assert result.onset_ms is not None
    assert abs(result.onset_ms - 250.0) < 1.0


def test_all_zero_file_is_silent() -> None:
    # Requirement 5.
    result = detect_onset_ms(np.zeros(SAMPLE_RATE, dtype=np.float64), SAMPLE_RATE)
    assert result.silent
    assert result.onset_ms is None


def test_low_level_noise_below_floor_is_silent() -> None:
    rng = np.random.default_rng(1234)
    noise = rng.normal(0.0, 1e-5, SAMPLE_RATE)
    result = detect_onset_ms(noise, SAMPLE_RATE)
    assert result.silent


def test_trailing_low_energy_tail_does_not_move_onset() -> None:
    # Requirement 14: tail appended after the body must not shift the onset.
    body = tone(duration_ms=700.0, onset_ms=200.0)
    tail_n = int(SAMPLE_RATE * 0.4)
    tail = np.full(tail_n, 0.002, dtype=np.float64)
    baseline = detect_onset_ms(body, SAMPLE_RATE)
    extended = detect_onset_ms(np.concatenate([body, tail]), SAMPLE_RATE)
    assert baseline.onset_ms is not None and extended.onset_ms is not None
    assert abs(extended.onset_ms - baseline.onset_ms) < 0.5


def test_fade_in_and_out_are_stable() -> None:
    # Requirement 14: fades must not be mistaken for silence or for the onset.
    fade = tone(duration_ms=900.0, onset_ms=300.0, fade_ms=25.0)
    hard_signal = tone(duration_ms=900.0, onset_ms=300.0)
    faded = detect_onset_ms(fade, SAMPLE_RATE)
    hard = detect_onset_ms(hard_signal, SAMPLE_RATE)
    assert faded.onset_ms is not None and hard.onset_ms is not None
    # A fade-in of 25 ms delays the point where the envelope clears the
    # threshold, but it must stay bounded and near the labelled edge.
    assert 300.0 <= faded.onset_ms <= 325.0
    assert abs(faded.onset_ms - hard.onset_ms) <= 25.0


def test_isolated_click_is_not_an_onset() -> None:
    # Single-sample spike shorter than the sustain window.
    samples = np.zeros(SAMPLE_RATE, dtype=np.float64)
    samples[1000] = 0.9
    samples[3000:3000 + 4410] = 0.4 * np.cos(2 * np.pi * 220 * np.arange(4410) / SAMPLE_RATE)
    result = detect_onset_ms(samples, SAMPLE_RATE)
    assert result.onset_ms is not None
    # ~3000 samples = 68.0 ms, not 1000 samples = 22.7 ms.
    assert result.onset_ms > 60.0


def test_click_only_file_yields_no_onset() -> None:
    samples = np.zeros(SAMPLE_RATE, dtype=np.float64)
    samples[1000] = 0.9
    result = detect_onset_ms(samples, SAMPLE_RATE)
    assert not result.silent  # signal exists, it just has no sustained onset
    assert result.onset_ms is None


def test_read_wav_16bit_roundtrip(tmp_path) -> None:
    samples = tone(duration_ms=100.0, onset_ms=10.0, amplitude=0.5)
    path = write_wav(tmp_path / "a.wav", samples)
    decoded = read_wav(path)
    assert decoded.info.sample_rate == SAMPLE_RATE
    assert decoded.info.channels == 1
    assert decoded.info.sample_width == 2
    assert decoded.samples.size == samples.size
    assert np.max(np.abs(decoded.samples - samples)) < 1e-3


def test_config_is_honoured() -> None:
    samples = tone(duration_ms=400.0, onset_ms=100.0)
    strict = detect_onset_ms(samples, SAMPLE_RATE, OnsetConfig(silence_floor=0.9))
    assert strict.silent
