import numpy as np
import pytest

from piano_workbench.dsp import (
    HOP,
    N_FFT,
    apply_operation,
    envelope_scale,
    fade,
    istft,
    patch_repair,
    spectral_denoise,
    stft,
)
from piano_workbench.pcm import float_to_pcm16, hash_pcm, pcm16_to_float


def test_stft_window_and_frame_constants():
    assert N_FFT == 2048
    assert HOP == 512
    win_length = stft(np.zeros(N_FFT * 2)).shape[1]
    assert win_length == N_FFT // 2 + 1


def test_stft_istft_roundtrip():
    rng = np.random.default_rng(7)
    signal = rng.standard_normal(12000).astype(np.float32) * 0.5
    spectrum = stft(signal)
    rebuilt = istft(spectrum, length=signal.size)
    # Interior samples reconstruct near-perfectly; edges are the
    # un-padded STFT boundary and are handled by operation-level crossfades.
    interior = slice(N_FFT, signal.size - N_FFT)
    assert np.max(np.abs(rebuilt[interior] - signal[interior])) < 1e-5


def test_float_to_pcm16_is_deterministic_and_bounded():
    values = np.array([-1.0, -0.5, 0.0, 0.5, 1.0, 2.0, -2.0, 1.5 / 32768.0],
                      dtype=np.float32)
    first = float_to_pcm16(values)
    second = float_to_pcm16(values)
    assert np.array_equal(first, second)
    assert first.min() >= -32768 and first.max() <= 32767
    # Explicit half-away-from-zero rule.
    assert float_to_pcm16(np.array([0.5 / 32768.0]))[0] == 1
    assert float_to_pcm16(np.array([-0.5 / 32768.0]))[0] == -1


def test_denoise_deterministic_and_localized(piano_wav):
    _, pcm = piano_wav
    start, end = 0, pcm.size // 4
    out_a = spectral_denoise(pcm16_to_float(pcm), start, end)
    out_b = spectral_denoise(pcm16_to_float(pcm), start, end)
    assert np.array_equal(float_to_pcm16(out_a), float_to_pcm16(out_b))
    # Samples outside the selection remain untouched.
    assert np.array_equal(out_a[end:], pcm16_to_float(pcm)[end:])
    # Denoise actually changes something inside the noisy selection.
    assert not np.array_equal(float_to_pcm16(out_a[start:end]), pcm[start:end])


def test_envelope_ramp_and_fade_shapes():
    pcm = float_to_pcm16(np.ones(1000, dtype=np.float32) * 0.5)
    scaled = envelope_scale(pcm16_to_float(pcm), 100, 900, 1.0, 0.0)
    assert scaled[99] == pcm16_to_float(pcm)[99]
    assert abs(float(scaled[899])) < 1e-6

    faded_in = fade(pcm16_to_float(pcm), 0, 500, mode="in")
    assert faded_in[0] == 0.0
    faded_out = fade(pcm16_to_float(pcm), 500, 1000, mode="out", curve="equal_power")
    assert abs(float(faded_out[-1])) < 1e-6


def test_patch_deterministic_and_only_selection_changes(piano_wav):
    _, pcm = piano_wav
    start, end = 3 * pcm.size // 4, pcm.size - 1024
    base = pcm16_to_float(pcm)
    a = patch_repair(base, start, end)
    b = patch_repair(base, start, end)
    assert np.array_equal(float_to_pcm16(a), float_to_pcm16(b))
    assert np.array_equal(a[:start], base[:start])
    assert np.array_equal(a[end:], base[end:])
    # The repair must actually reconstruct the selection rather than no-op.
    assert np.count_nonzero(np.abs(a[start:end] - base[start:end])) > 0


def test_patch_seeded_phase_changes_with_seed_but_is_stable(piano_wav):
    _, pcm = piano_wav
    start, end = 3 * pcm.size // 4, pcm.size - 1024
    base = pcm16_to_float(pcm)
    seed_a = patch_repair(base, start, end, seed=1)
    seed_b = patch_repair(base, start, end, seed=2)
    assert not np.array_equal(float_to_pcm16(seed_a), float_to_pcm16(seed_b))
    assert np.array_equal(seed_a, patch_repair(base, start, end, seed=1))


def test_apply_operation_preserves_length_and_is_byte_stable(piano_wav):
    _, pcm = piano_wav
    for op_type, params, selection in [
        ("denoise", {"seed": 1}, (0, pcm.size // 4)),
        ("envelope", {"gain_start": 0.8, "gain_end": 0.2},
         (pcm.size // 4, pcm.size // 2)),
        ("fade", {"mode": "out"}, (pcm.size // 2, 3 * pcm.size // 4)),
        ("patch", {"gl_iterations": 8},
         (3 * pcm.size // 4, pcm.size - 1024)),
    ]:
        out_a = apply_operation(pcm, op_type, selection, params)
        out_b = apply_operation(pcm, op_type, selection, params)
        assert out_a.dtype == "<i2"
        assert out_a.shape == pcm.shape
        assert np.array_equal(out_a, out_b)
        assert hash_pcm(out_a) == hash_pcm(out_b)


def test_unknown_operation_raises_value_error_at_dsp_layer(piano_wav):
    _, pcm = piano_wav
    with pytest.raises(ValueError):
        apply_operation(pcm, "nonsense", (0, 100), {})
