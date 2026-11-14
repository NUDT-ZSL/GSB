"""WAV decoding and energy-envelope onset detection.

All arithmetic is done on float64 numpy arrays.  The decoder returns samples in
the range [-1, 1] together with the container metadata so the caller can decide
whether the file is acceptable for verification.
"""

from __future__ import annotations

import wave
from dataclasses import dataclass
from pathlib import Path

import numpy as np

# Sample data narrower than this is not considered to carry signal at all.
_PCM_MIN_WIDTH = 1
_PCM_MAX_WIDTH = 4


class WavError(Exception):
    """Raised when a WAV cannot be read or has an unsupported encoding."""


@dataclass(frozen=True)
class WavInfo:
    path: str
    sample_rate: int
    channels: int
    sample_width: int
    frames: int

    @property
    def duration_ms(self) -> float:
        if self.sample_rate <= 0:
            return 0.0
        return self.frames * 1000.0 / self.sample_rate


@dataclass(frozen=True)
class DecodedWav:
    info: WavInfo
    samples: np.ndarray  # float64, mono (channels==1)


def _pcm_to_float(raw: bytes, sample_width: int) -> np.ndarray:
    """Convert little-endian PCM bytes to float64 in [-1, 1]."""
    if sample_width == 1:
        # 8-bit PCM is unsigned with a 128 offset.
        data = np.frombuffer(raw, dtype=np.uint8).astype(np.float64)
        return (data - 128.0) / 128.0
    if sample_width == 2:
        data = np.frombuffer(raw, dtype="<i2").astype(np.float64)
        return data / 32768.0
    if sample_width == 3:
        raw = np.frombuffer(raw, dtype=np.uint8)
        usable = (raw.size // 3) * 3
        raw = raw[:usable].reshape(-1, 3).astype(np.int32)
        data = raw[:, 0] | (raw[:, 1] << 8) | (raw[:, 2] << 16)
        # Sign-extend 24-bit values.
        data = np.where(data & 0x800000, data - 0x1000000, data)
        return data.astype(np.float64) / 8388608.0
    if sample_width == 4:
        data = np.frombuffer(raw, dtype="<i4").astype(np.float64)
        return data / 2147483648.0
    raise WavError(f"unsupported PCM sample width: {sample_width} bytes")


def read_wav(path: str | Path) -> DecodedWav:
    """Decode a PCM WAV file.

    Raises :class:`WavError` for anything that cannot be decoded (missing file,
    compressed encoding, truncated data).  Sample-rate and channel validation
    is deliberately *not* done here so the caller can classify the line as
    unverifiable rather than crashing the whole run.
    """
    p = Path(path)
    if not p.is_file():
        raise WavError(f"file not found: {p}")
    try:
        with wave.open(str(p), "rb") as wf:
            info = WavInfo(
                path=str(p),
                sample_rate=wf.getframerate(),
                channels=wf.getnchannels(),
                sample_width=wf.getsampwidth(),
                frames=wf.getnframes(),
            )
            raw = wf.readframes(info.frames)
    except (wave.Error, EOFError, OSError) as exc:
        raise WavError(f"cannot decode WAV {p}: {exc}") from exc

    if not (_PCM_MIN_WIDTH <= info.sample_width <= _PCM_MAX_WIDTH):
        raise WavError(f"unsupported sample width {info.sample_width * 8}-bit: {p}")

    samples = _pcm_to_float(raw, info.sample_width)
    if info.channels > 1:
        frame_count = samples.size // info.channels
        samples = samples[: frame_count * info.channels].reshape(frame_count, info.channels)
    return DecodedWav(info=info, samples=samples)


def _pcm_peak(samples: np.ndarray) -> float:
    """Peak absolute amplitude across the whole file."""
    if samples.size == 0:
        return 0.0
    return float(np.max(np.abs(samples)))


@dataclass(frozen=True)
class OnsetConfig:
    # Width of the moving-RMS window in milliseconds.  Small windows give
    # sub-millisecond localisation while smoothing single-sample noise.
    envelope_ms: float = 1.0
    # The envelope must stay above threshold for this long to count as an
    # onset ("sustained" condition, requirement 2).
    sustain_ms: float = 4.0
    # Absolute digital silence floor; a file whose peak is below this is
    # considered silent (requirement 5).
    silence_floor: float = 1e-4
    # Relative to the file's peak: the envelope must exceed peak * this value.
    rel_threshold: float = 0.02
    # Absolute lower bound for the threshold so quiet-but-real files detect.
    min_threshold: float = 2e-4
    # Leading/trailing guard ignored when deriving the peak used for the
    # relative threshold, so fade-in/out extremes and trailing tails cannot
    # move the onset (requirement 14).
    edge_guard_ms: float = 10.0


@dataclass(frozen=True)
class OnsetResult:
    onset_ms: float | None
    # True only when the file's whole-signal peak is below the silence floor.
    silent: bool
    threshold: float


def _moving_rms(mono: np.ndarray, window: int) -> np.ndarray:
    """Moving RMS with a centred-ish window, computed via cumulative sums."""
    squared = np.square(mono)
    csum = np.concatenate(([0.0], np.cumsum(squared)))
    n = mono.size
    # Envelope value at index i covers samples [i - window + 1, i] (causal),
    # which means the first sample that can carry a value is the first sample
    # at which signal has actually been seen -- no look-ahead bias.
    starts = np.maximum(0, np.arange(n) - window + 1)
    ends = np.arange(n) + 1
    sums = csum[ends] - csum[starts]
    lengths = ends - starts
    return np.sqrt(sums / lengths)


def detect_onset_ms(samples: np.ndarray, sample_rate: int, config: OnsetConfig | None = None) -> OnsetResult:
    """Return the first sustained non-silent position in milliseconds.

    A moving-RMS energy envelope is computed over the whole file.  The
    detection threshold is relative to the peak of the *interior* of the file
    (edges ignored) so trailing low-energy tails and fade-outs cannot shift
    it.  An onset is only reported once the envelope has stayed above the
    threshold for ``sustain_ms``, which keeps isolated clicks from counting.
    """
    cfg = config or OnsetConfig()
    if samples.size == 0 or sample_rate <= 0:
        return OnsetResult(onset_ms=None, silent=True, threshold=0.0)

    mono = samples
    if mono.ndim == 2:
        # Accept interleaved stereo for analysis if the caller chooses to.
        mono = np.mean(mono, axis=1)
    mono = np.asarray(mono, dtype=np.float64)

    # Digital silence of the entire file: report silent regardless of anything
    # else (requirement 5).
    file_peak = _pcm_peak(mono)
    if file_peak < cfg.silence_floor:
        return OnsetResult(onset_ms=None, silent=True, threshold=cfg.silence_floor)

    window = max(1, int(round(sample_rate * cfg.envelope_ms / 1000.0)))
    envelope = _moving_rms(mono, window)

    # Peak used for the relative threshold excludes the leading/trailing guard.
    guard = int(round(sample_rate * cfg.edge_guard_ms / 1000.0))
    if mono.size > 2 * guard + 1:
        interior = envelope[guard : mono.size - guard]
    else:
        interior = envelope
    peak = float(np.max(interior)) if interior.size else float(np.max(envelope))
    if not np.isfinite(peak) or peak <= 0.0:
        return OnsetResult(onset_ms=None, silent=True, threshold=cfg.silence_floor)

    threshold = max(cfg.min_threshold, peak * cfg.rel_threshold)
    if threshold >= peak:
        # Degenerate case: the relative threshold swallowed the signal.
        threshold = peak * 0.5

    above = envelope > threshold
    if not bool(np.any(above)):
        # Signal exists but never clears the detection threshold.
        return OnsetResult(onset_ms=None, silent=False, threshold=threshold)

    # Find the first index from which the envelope stays above threshold for
    # the whole sustain window: the run starting at i is sustained iff every
    # sample in [i, i + sustain) is above threshold.
    sustain = max(1, int(round(sample_rate * cfg.sustain_ms / 1000.0)))
    n = above.size
    sustained_start = None
    if sustain <= n:
        counts = np.concatenate(([0], np.cumsum(above.astype(np.int64))))
        window_counts = counts[sustain:] - counts[:-sustain]
        candidates = np.nonzero(window_counts == sustain)[0]
        if candidates.size:
            sustained_start = int(candidates[0])

    if sustained_start is None:
        # Only impulsive energy shorter than the sustain window: not a line onset.
        return OnsetResult(onset_ms=None, silent=False, threshold=threshold)

    onset_ms = sustained_start * 1000.0 / sample_rate
    return OnsetResult(onset_ms=float(onset_ms), silent=False, threshold=threshold)
