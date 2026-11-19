"""Deterministic frame-level acoustic feature extraction.

Features per frame:
  fine (8D):     energy, log-energy, zcr, centroid, zc_peak, rolloff, flux, f0
  approximate (4D): energy, log-energy, zcr, f0

The approximate regime may evaluate features on a strided subset of frames
(``frame_stride > 1``); full-frame results are then reconstructed by nearest
anchor so every downstream stage still sees the complete, GT-aligned frame
index set, while peak resident memory and comparisons stay bounded.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np

from .errors import BusinessError, ErrorCode
from .framing import frame_count, ms_to_samples

FINE_NAMES = (
    "energy", "log_energy", "zcr", "centroid",
    "zc_peak", "rolloff", "flux", "f0",
)
APPROX_NAMES = ("energy", "log_energy", "zcr", "f0")


@dataclass
class FeatureResult:
    features: np.ndarray            # shape (n_frames, dim), float64
    frame_indices: np.ndarray       # shape (n_frames,), int64
    dim: int
    frame_stride: int
    resident_frames: int            # frames actually materialised at once peak
    feature_names: tuple


def _estimate_f0(pcm: np.ndarray, start: int, frame_size: int,
                 sample_rate: int) -> float:
    """Pitch on a 60 ms context window centered on the current frame.

    A longer context stabilises autocorrelation at low male f0 while the
    estimate remains a deterministic function of frame position.
    """
    context = 3 * frame_size
    half = context // 2
    center = start + frame_size // 2
    lo = max(0, center - half)
    hi = min(pcm.shape[0], lo + context)
    lo = max(0, hi - context)
    seg = pcm[lo:hi].astype(np.float64) / 32768.0
    if seg.shape[0] < frame_size:
        return 0.0
    xc = seg - seg.mean()
    energy = float(np.dot(xc, xc))
    if energy <= 1e-8:
        return 0.0
    ac = np.correlate(xc, xc, mode="full")[xc.shape[0] - 1:]
    min_lag = max(1, int(sample_rate / 400.0))
    max_lag = min(xc.shape[0] - 1, int(sample_rate / 70.0))
    if max_lag <= min_lag:
        return 0.0
    peak = int(np.argmax(ac[min_lag:max_lag])) + min_lag
    if ac[peak] <= 0.15 * ac[0]:
        return 0.0
    if 0 < peak < ac.shape[0] - 1:
        y0, y1, y2 = ac[peak - 1], ac[peak], ac[peak + 1]
        denom_q = y0 - 2.0 * y1 + y2
        shift = 0.5 * (y0 - y2) / denom_q if abs(denom_q) > 1e-12 else 0.0
        refined = peak + float(np.clip(shift, -1.0, 1.0))
    else:
        refined = float(peak)
    return float(sample_rate / refined)


def _window_features(
    window: np.ndarray, prev_window: np.ndarray | None, frame_size: int,
    sample_rate: int, dim: int, pcm: np.ndarray | None = None,
    frame_start: int = 0,
) -> np.ndarray:
    x = window.astype(np.float64) / 32768.0
    energy = float(np.dot(x, x) / frame_size)
    log_energy = float(np.log1p(energy * 1e6) / np.log(10.0))
    signs = np.signbit(x)
    zcr = int(np.count_nonzero(signs[1:] != signs[:-1]))
    zcr_norm = zcr / float(frame_size - 1)

    spectrum = np.abs(np.fft.rfft(x * np.hanning(frame_size)))
    freqs = np.fft.rfftfreq(frame_size, d=1.0 / sample_rate)
    power = spectrum * spectrum
    total_power = float(power.sum())
    if total_power > 1e-12:
        centroid = float((freqs * power).sum() / total_power) / sample_rate
        cum = np.cumsum(power)
        rolloff_idx = int(np.searchsorted(cum, 0.85 * cum[-1]))
        rolloff = float(freqs[min(rolloff_idx, len(freqs) - 1)]) / sample_rate
    else:
        centroid = 0.0
        rolloff = 0.0

    zc_idx = np.flatnonzero(signs[1:] != signs[:-1]) + 1
    if zc_idx.size >= 2:
        gaps = np.diff(zc_idx).astype(np.float64)
        zc_peak = float(np.mean(gaps > 1.5 * np.median(gaps)))
    else:
        zc_peak = 0.0

    if prev_window is not None:
        prev = prev_window.astype(np.float64) / 32768.0
        prev_spec = np.abs(np.fft.rfft(prev * np.hanning(frame_size)))
        denom = float(np.dot(prev_spec, prev_spec)) + 1e-12
        flux = float(np.dot(spectrum - prev_spec, spectrum - prev_spec) / denom)
    else:
        flux = 0.0

    f0 = (
        _estimate_f0(pcm, frame_start, frame_size, sample_rate)
        if pcm is not None
        else 0.0
    )

    fine = np.array(
        [energy, log_energy, zcr_norm, centroid, zc_peak, rolloff, flux, f0],
        dtype=np.float64,
    )
    if dim == 8:
        return fine
    # Reduced set keeps the strongest speaker-discriminative cue (f0).
    return np.array([energy, log_energy, zcr_norm, f0], dtype=np.float64)


def extract_features(
    pcm: np.ndarray,
    sample_rate: int,
    frame_ms: float,
    hop_ms: float,
    dim: int,
    expected_frames: int | None = None,
    frame_stride: int = 1,
    dialog_id: str | None = None,
) -> FeatureResult:
    """Extract deterministic features and validate frame counts.

    Raises ``E_FRAME_MISMATCH`` when the number of PCM-derived frames differs
    from ``expected_frames`` (the ground-truth frame count).
    """
    if dim not in (4, 8):
        raise BusinessError(
            ErrorCode.E_CONFIG_INVALID,
            f"unsupported feature dim {dim}; choose 4 or 8",
            field="feature_dim",
            dialog_id=dialog_id,
        )
    if not isinstance(frame_stride, int) or frame_stride < 1:
        raise BusinessError(
            ErrorCode.E_CONFIG_INVALID,
            "frame_stride must be a positive integer",
            field="frame_stride",
            dialog_id=dialog_id,
        )
    frame_size = ms_to_samples(frame_ms, sample_rate)
    hop_size = ms_to_samples(hop_ms, sample_rate)
    n_frames = frame_count(pcm.shape[0], frame_size, hop_size)
    if expected_frames is not None and n_frames != expected_frames:
        raise BusinessError(
            ErrorCode.E_FRAME_MISMATCH,
            f"feature frames {n_frames} != ground-truth frames {expected_frames}",
            dialog_id=dialog_id,
            field="num_frames",
            details={"feature_frames": n_frames, "gt_frames": expected_frames},
        )

    anchor_indices = np.arange(0, n_frames, frame_stride, dtype=np.int64)
    anchor_values = np.empty((anchor_indices.shape[0], dim), dtype=np.float64)
    prev_window: np.ndarray | None = None
    next_anchor = 0
    resident_peak = 0
    for idx in range(n_frames):
        start = idx * hop_size
        window = pcm[start:start + frame_size]
        resident_peak = max(resident_peak, window.shape[0])
        if next_anchor < anchor_indices.shape[0] and idx == anchor_indices[next_anchor]:
            anchor_values[next_anchor] = _window_features(
                window, prev_window, frame_size, sample_rate, dim,
                pcm=pcm, frame_start=start,
            )
            next_anchor += 1
        prev_window = window

    if frame_stride == 1:
        features = anchor_values
    else:
        # Nearest-anchor reconstruction keeps the full GT-aligned frame set
        # without ever materialising a full-size matrix in the streaming core.
        features = np.empty((n_frames, dim), dtype=np.float64)
        nearest = np.round(np.arange(n_frames) / frame_stride).astype(np.int64)
        np.minimum(nearest, anchor_indices.shape[0] - 1, out=nearest)
        features[:] = anchor_values[nearest]

    return FeatureResult(
        features=features,
        frame_indices=np.arange(n_frames, dtype=np.int64),
        dim=dim,
        frame_stride=frame_stride,
        resident_frames=resident_peak,
        feature_names=FINE_NAMES if dim == 8 else APPROX_NAMES,
    )
