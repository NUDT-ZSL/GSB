"""Deterministic NumPy DSP primitives and the four repair operations.

STFT convention (fixed for the whole project):
    window length N = 2048, hop H = 512, periodic Hann window, rfft frames.

Everything here is pure NumPy and uses explicit fixed seeds; no global RNG
state is touched, so repeated calls with identical inputs and parameters yield
bit-identical int16 PCM output.
"""

from __future__ import annotations

import numpy as np

from .pcm import float_to_pcm16, pcm16_to_float

N_FFT = 2048
HOP = 512
NOISE_SEED = 20240517
PATCH_GL_ITERATIONS = 32


# --------------------------------------------------------------------------- #
# STFT / inverse STFT
# --------------------------------------------------------------------------- #
def hann_window(n: int = N_FFT) -> np.ndarray:
    """Periodic Hann window (scipy.signal.get_window(' hann', n, fftbins=True))."""

    idx = np.arange(n, dtype=np.float64)
    return (0.5 - 0.5 * np.cos(2.0 * np.pi * idx / n)).astype(np.float32)


def stft(signal: np.ndarray, n_fft: int = N_FFT, hop: int = HOP) -> np.ndarray:
    """Forward STFT.

    Returns a complex64 array shaped ``(n_frames, n_fft // 2 + 1)``. Signals
    shorter than one window are zero-padded so the transform always exists.
    """

    x = np.asarray(signal, dtype=np.float32)
    if x.size < n_fft:
        x = np.pad(x, (0, n_fft - x.size))
    n_frames = 1 + (x.size - n_fft) // hop
    win = hann_window(n_fft)
    shape = (n_frames, n_fft)
    strides = (x.strides[0] * hop, x.strides[0])
    frames = np.lib.stride_tricks.as_strided(x, shape=shape, strides=strides)
    return np.fft.rfft(frames * win, n=n_fft, axis=1).astype(np.complex64)


def istft(
    spectrum: np.ndarray,
    length: int | None = None,
    n_fft: int = N_FFT,
    hop: int = HOP,
) -> np.ndarray:
    """Inverse STFT with Hann-window overlap-add normalization."""

    n_frames = spectrum.shape[0]
    win = hann_window(n_fft)
    out_len = n_fft + (n_frames - 1) * hop
    output = np.zeros(out_len, dtype=np.float64)
    frames = np.fft.irfft(spectrum, n=n_fft, axis=1)
    for frame_idx in range(n_frames):
        start = frame_idx * hop
        end = start + n_fft
        output[start:end] += frames[frame_idx] * win
    # Constant-overlap-add normalization of the periodic Hann window.
    output *= hop / float(np.sum(win * win))
    if length is not None:
        if output.size < length:
            output = np.pad(output, (0, length - output.size))
        output = output[:length]
    return output.astype(np.float32)


def _blend_crossfade(original: np.ndarray, repaired: np.ndarray, fade: int) -> np.ndarray:
    """Equal-power crossfade at the two edges of two equal-length arrays."""

    fade = max(0, min(fade, original.size // 2))
    if fade == 0:
        return repaired.copy()
    ramp = 0.5 - 0.5 * np.cos(np.linspace(0.0, np.pi, fade, endpoint=False))
    weight = np.ones(original.size, dtype=np.float32)
    weight[:fade] = ramp
    weight[-fade:] = ramp[::-1]
    return original * (1.0 - weight) + repaired * weight


# --------------------------------------------------------------------------- #
# Operation 1: spectral subtraction denoise (fixed noise-estimation seed)
# --------------------------------------------------------------------------- #
def spectral_denoise(
    signal: np.ndarray,
    start: int,
    end: int,
    over_subtraction: float = 2.0,
    floor: float = 0.02,
    noise_frame_count: int = 12,
    seed: int = NOISE_SEED,
    n_fft: int = N_FFT,
    hop: int = HOP,
) -> np.ndarray:
    """Spectral subtraction restricted to ``[start, end)``.

    The noise profile is the average magnitude of frames drawn by a seeded
    ``numpy.random.default_rng`` from the first quarter of the selection, so
    the estimate is fully deterministic and needs no external noise asset.
    """

    result = np.array(signal, dtype=np.float32, copy=True)
    segment = result[start:end].copy()
    if segment.size < n_fft:
        return result

    spec = stft(segment, n_fft, hop)
    magnitudes = np.abs(spec)
    n_frames = spec.shape[0]
    candidate = max(1, n_frames // 4)
    pick = min(noise_frame_count, candidate)
    rng = np.random.default_rng(seed)
    chosen = np.sort(rng.choice(candidate, size=pick, replace=False))
    noise_profile = magnitudes[chosen].mean(axis=0)

    denoised_mag = magnitudes - np.float32(over_subtraction) * noise_profile
    floored = np.maximum(denoised_mag, np.float32(floor) * magnitudes)
    phase = np.exp(1j * np.angle(spec)).astype(np.complex64)
    rebuilt = istft(floored * phase, length=segment.size, n_fft=n_fft, hop=hop)

    fade = min(hop, segment.size // 4)
    result[start:end] = _blend_crossfade(segment, rebuilt, fade)
    return result


# --------------------------------------------------------------------------- #
# Operation 2: envelope amplitude scaling (constant or linear ramp)
# --------------------------------------------------------------------------- #
def envelope_scale(
    signal: np.ndarray,
    start: int,
    end: int,
    gain_start: float,
    gain_end: float | None = None,
) -> np.ndarray:
    """Scale ``[start, end)`` by a gain value or a linear gain ramp."""

    result = np.array(signal, dtype=np.float32, copy=True)
    length = end - start
    if gain_end is None:
        ramp = np.full(length, np.float32(gain_start), dtype=np.float32)
    else:
        ramp = np.linspace(
            np.float32(gain_start), np.float32(gain_end), length, dtype=np.float32
        )
    result[start:end] = result[start:end] * ramp
    return result


# --------------------------------------------------------------------------- #
# Operation 3: fade in / fade out
# --------------------------------------------------------------------------- #
def fade(
    signal: np.ndarray,
    start: int,
    end: int,
    mode: str = "in",
    curve: str = "linear",
) -> np.ndarray:
    """Apply a fade-in or fade-out over ``[start, end)``."""

    result = np.array(signal, dtype=np.float32, copy=True)
    length = end - start
    positions = np.linspace(0.0, 1.0, length, endpoint=False, dtype=np.float32)
    if curve == "equal_power":
        ramp = np.sqrt(positions)
    else:
        ramp = positions
    if mode == "out":
        ramp = ramp[::-1]
    result[start:end] = result[start:end] * ramp
    return result


# --------------------------------------------------------------------------- #
# Operation 4: STFT magnitude interpolation + Griffin-Lim phase patch
# --------------------------------------------------------------------------- #
PATCH_PHASE_SEED = 12345


def patch_repair(
    signal: np.ndarray,
    start: int,
    end: int,
    margin: int = N_FFT,
    gl_iterations: int = PATCH_GL_ITERATIONS,
    seed: int = PATCH_PHASE_SEED,
    n_fft: int = N_FFT,
    hop: int = HOP,
) -> np.ndarray:
    """Locally reconstruct a damaged region.

    Magnitudes of frames touching the selection are linearly interpolated from
    the nearest fully-exterior anchor frames on each side. The phase is rebuilt
    with a fixed number of deterministic Griffin-Lim iterations starting from a
    seeded random phase (a fixed-point-free initialization that prevents the
    iteration from collapsing back onto the original damaged signal). Exterior
    frames are pinned to the original spectrum on every iteration. Only samples
    inside ``[start, end)`` are replaced, with an internal equal-power edge
    crossfade; the margins are restored byte-for-byte.
    """

    result = np.array(signal, dtype=np.float32, copy=True)
    region_start = max(0, start - margin)
    region_end = min(signal.size, end + margin)
    region = result[region_start:region_end].copy()
    local_start = start - region_start
    local_end = end - region_start

    spec = stft(region, n_fft, hop)
    magnitude = np.abs(spec)
    n_frames = spec.shape[0]
    frame_starts = np.arange(n_frames) * hop
    frame_ends = frame_starts + n_fft

    interior = (frame_starts < local_end) & (frame_ends > local_start)
    exterior = ~interior
    interior_idx = np.flatnonzero(interior)
    exterior_idx = np.flatnonzero(exterior)
    if interior_idx.size == 0 or exterior_idx.size == 0:
        # No frame fully outside the selection: nothing to anchor against.
        return result

    left_anchor = exterior_idx[exterior_idx < interior_idx[0]]
    right_anchor = exterior_idx[exterior_idx > interior_idx[-1]]
    if left_anchor.size:
        left_mag = magnitude[left_anchor[-1]]
    else:
        # No fully-exterior frame on the left: use the first boundary frame.
        left_mag = magnitude[interior_idx[0]]
    if right_anchor.size:
        right_mag = magnitude[right_anchor[0]]
    else:
        # No fully-exterior frame on the right (selection near clip end):
        # use the last boundary frame so interpolation still moves interior
        # magnitudes away from their damaged original values.
        right_mag = magnitude[interior_idx[-1]]

    target_magnitude = magnitude.copy()
    span = float(interior_idx.size + 1)
    for step, frame_idx in enumerate(interior_idx, start=1):
        alpha = np.float32(step / span)
        target_magnitude[frame_idx] = (1.0 - alpha) * left_mag + alpha * right_mag

    rng = np.random.default_rng(seed)
    initial_phase = rng.uniform(
        -np.pi, np.pi, size=spec.shape
    ).astype(np.float32)
    rebuilt = target_magnitude * np.exp(1j * initial_phase)
    for _ in range(int(gl_iterations)):
        time_domain = istft(rebuilt, length=region.size, n_fft=n_fft, hop=hop)
        re_analyzed = stft(time_domain, n_fft, hop)
        re_phase = np.exp(1j * np.angle(re_analyzed)).astype(np.complex64)
        rebuilt = target_magnitude * re_phase
        rebuilt[exterior] = spec[exterior]

    region_rebuilt = istft(rebuilt, length=region.size, n_fft=n_fft, hop=hop)

    # Replace only the selection; crossfade weights stay strictly inside it,
    # so every sample outside [start, end) remains identical to the parent.
    sel_length = local_end - local_start
    edge_fade = min(hop, sel_length // 2)
    ramp = 0.5 - 0.5 * np.cos(np.linspace(0.0, np.pi, edge_fade, endpoint=False))
    weight = np.ones(sel_length, dtype=np.float32)
    weight[:edge_fade] = ramp
    weight[-edge_fade:] = ramp[::-1]
    original_selection = region[local_start:local_end]
    rebuilt_selection = region_rebuilt[local_start:local_end]
    region[local_start:local_end] = (
        original_selection * (1.0 - weight) + rebuilt_selection * weight
    )

    result[region_start:region_end] = region
    result[:region_start] = signal[:region_start]
    result[region_end:] = signal[region_end:]
    return result


# --------------------------------------------------------------------------- #
# Dispatch + quantized node application
# --------------------------------------------------------------------------- #
def apply_operation_float(
    pcm_parent: np.ndarray, op_type: str, selection: tuple[int, int], params: dict
) -> np.ndarray:
    """Apply one operation to parent int16 PCM; return float32 waveform."""

    start, end = selection
    base = pcm16_to_float(pcm_parent)
    if op_type == "denoise":
        return spectral_denoise(
            base,
            start,
            end,
            over_subtraction=float(params.get("over_subtraction", 2.0)),
            floor=float(params.get("floor", 0.02)),
            noise_frame_count=int(params.get("noise_frame_count", 12)),
            seed=int(params.get("seed", NOISE_SEED)),
        )
    if op_type == "envelope":
        return envelope_scale(
            base,
            start,
            end,
            gain_start=float(params["gain_start"]),
            gain_end=(None if params.get("gain_end") is None else float(params["gain_end"])),
        )
    if op_type == "fade":
        return fade(
            base,
            start,
            end,
            mode=str(params.get("mode", "in")),
            curve=str(params.get("curve", "linear")),
        )
    if op_type == "patch":
        return patch_repair(
            base,
            start,
            end,
            margin=int(params.get("margin", N_FFT)),
            gl_iterations=int(params.get("gl_iterations", PATCH_GL_ITERATIONS)),
            seed=int(params.get("seed", PATCH_PHASE_SEED)),
        )
    raise ValueError(f"unknown op_type: {op_type}")  # converted to E_PARAM upstream


def apply_operation(
    pcm_parent: np.ndarray, op_type: str, selection: tuple[int, int], params: dict
) -> np.ndarray:
    """Apply one operation and return the deterministic int16 PCM result."""

    return float_to_pcm16(apply_operation_float(pcm_parent, op_type, selection, params))
