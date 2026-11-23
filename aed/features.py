"""Node 1: deterministic frame-level acoustic feature extraction."""

from __future__ import annotations

import numpy as np

from .config import PipelineConfig
from .errors import AEDError, Code

N_FEATURES = 4  # energy, zcr, spectral centroid, spectral rolloff


def frame_count(n_samples: int, cfg: PipelineConfig) -> int:
    if n_samples < cfg.frame_len:
        return 0
    return 1 + (n_samples - cfg.frame_len) // cfg.hop


def frame_bounds(frame_index: int, cfg: PipelineConfig):
    start = frame_index * cfg.hop
    return start, start + cfg.frame_len


def extract_features(pcm: np.ndarray, cfg: PipelineConfig,
                     dialog_id: str, duration_ms: int) -> np.ndarray:
    """Return an (n_frames, 4) float64 feature matrix.

    Frame boundaries are exact sample indexes. Raises E_FRAME_MISMATCH
    (naming the dialog) when the sample count disagrees with duration_ms.
    """
    cfg.validate()
    expected = duration_ms * cfg.sample_rate // 1000
    if len(pcm) != expected:
        raise AEDError(Code.E_FRAME_MISMATCH,
                       "sample count inconsistent with duration_ms",
                       dialog_id=dialog_id, n_samples=len(pcm),
                       expected_samples=expected, duration_ms=duration_ms)
    n_frames = frame_count(len(pcm), cfg)
    if n_frames > cfg.max_frames_resident:
        raise AEDError(Code.E_RESOURCE_LIMIT,
                       "frame feature residency limit exceeded",
                       n_frames=n_frames, limit=cfg.max_frames_resident)

    x = pcm.astype(np.float64) / 32768.0
    frames = np.lib.stride_tricks.as_strided(
        x, shape=(n_frames, cfg.frame_len),
        strides=(x.strides[0] * cfg.hop, x.strides[0])).copy()
    window = np.hanning(cfg.frame_len)
    spec = np.abs(np.fft.rfft(frames * window, axis=1))
    freqs = np.fft.rfftfreq(cfg.frame_len, 1.0 / cfg.sample_rate)

    energy = np.sum(frames ** 2, axis=1) / cfg.frame_len
    signs = np.signbit(frames)
    zcr = np.sum(signs[:, 1:] != signs[:, :-1], axis=1) / (cfg.frame_len - 1)

    spec_sum = spec.sum(axis=1)
    safe = np.where(spec_sum > 0, spec_sum, 1.0)
    centroid = (spec * freqs).sum(axis=1) / safe

    cum = np.cumsum(spec, axis=1)
    thresh = 0.85 * np.where(spec_sum > 0, spec_sum, -1.0)[:, None]
    idx = (cum >= thresh).argmax(axis=1)
    rolloff = np.where(spec_sum > 0, freqs[idx], 0.0)

    feats = np.stack([energy, zcr, centroid, rolloff], axis=1)
    return np.ascontiguousarray(feats, dtype=np.float64)
