"""Deterministic frame-level acoustic feature extraction."""

from __future__ import annotations

import math
from typing import Sequence

import numpy as np

from .catalog import event_type_id
from .codes import BusinessCode
from .config import FEATURE_NAMES_FULL, FEATURE_NAMES_REDUCED, FeatureConfig, validate_feature_config
from .errors import AedError
from .types import FeatureResult


def frame_count_for_samples(n_samples: int, hop_samples: int) -> int:
    """Canonical frame count: every sample belongs to exactly one hop cell.

    The final feature window is zero-padded when it exceeds the PCM length.
    """

    if n_samples <= 0:
        return 0
    if hop_samples <= 0:
        raise ValueError("hop_samples must be positive")
    return int(math.ceil(n_samples / hop_samples))


def extract_features(
    pcm: np.ndarray | Sequence[float],
    config: FeatureConfig,
    *,
    dialog_id: str,
    truth_frame_count: int | None = None,
) -> FeatureResult:
    """Extract frame features and enforce truth/frame-count alignment.

    Frame ``i`` starts at sample ``i * hop_samples``. The window is
    ``frame_samples`` long and is zero-padded only at the end of the signal.
    """

    validate_feature_config(config, dialog_id=dialog_id)
    signal = np.asarray(pcm, dtype=np.float64)
    if signal.ndim != 1:
        raise AedError(
            BusinessCode.E_CONFIG_INVALID,
            "PCM must be mono (one-dimensional)",
            dialog_id=dialog_id,
            field="pcm",
        )
    if signal.size == 0:
        raise AedError(
            BusinessCode.E_CONFIG_INVALID,
            "PCM must contain at least one sample",
            dialog_id=dialog_id,
            field="pcm",
        )
    if not np.all(np.isfinite(signal)):
        raise AedError(
            BusinessCode.E_CONFIG_INVALID,
            "PCM contains NaN or infinity",
            dialog_id=dialog_id,
            field="pcm",
        )

    frame_samples = config.frame_samples
    hop_samples = config.hop_samples
    sample_rate = config.sample_rate
    frame_count = frame_count_for_samples(signal.size, hop_samples)
    if truth_frame_count is not None:
        if (
            isinstance(truth_frame_count, bool)
            or not isinstance(truth_frame_count, int)
            or truth_frame_count < 0
        ):
            raise AedError(
                BusinessCode.E_CONFIG_INVALID,
                "truth_frame_count must be a non-negative integer",
                dialog_id=dialog_id,
                field="truth_frame_count",
            )
        if frame_count != truth_frame_count:
            raise AedError(
                BusinessCode.E_FRAME_MISMATCH,
                f"feature frame count {frame_count} does not match truth frame count "
                f"{truth_frame_count} for dialog {dialog_id}",
                dialog_id=dialog_id,
                field="frame_count",
                details={
                    "actual_frame_count": frame_count,
                    "truth_frame_count": truth_frame_count,
                },
            )

    starts = np.arange(frame_count, dtype=np.int64) * hop_samples
    ends = np.minimum(starts + frame_samples, signal.size)
    all_features = np.zeros((frame_count, len(FEATURE_NAMES_FULL)), dtype=np.float64)
    window = np.hanning(frame_samples).astype(np.float64)
    n_fft = config.n_fft
    frequencies = np.fft.rfftfreq(n_fft, d=1.0 / sample_rate)
    low_mask = frequencies < 500.0
    high_mask = frequencies >= 3000.0
    previous_spectrum = np.zeros_like(frequencies)

    for frame_index, start in enumerate(starts):
        raw = np.zeros(frame_samples, dtype=np.float64)
        available = signal[start : min(start + frame_samples, signal.size)]
        raw[: available.size] = available

        energy = float(np.mean(raw * raw))
        log_energy = 10.0 * math.log10(energy + config.eps)

        if frame_samples > 1:
            zcr = float(np.count_nonzero(np.signbit(raw[1:]) != np.signbit(raw[:-1]))) / (
                frame_samples - 1
            )
        else:
            zcr = 0.0

        spectrum = np.abs(np.fft.rfft(raw * window, n=n_fft))
        magnitude_sum = float(np.sum(spectrum))
        power = spectrum * spectrum
        power_sum = float(np.sum(power))
        if magnitude_sum > config.eps:
            centroid = float(np.sum(frequencies * spectrum) / magnitude_sum)
            cumulative = np.cumsum(spectrum)
            rolloff_index = int(
                np.searchsorted(cumulative, config.rolloff_ratio * cumulative[-1], side="left")
            )
            rolloff_index = min(rolloff_index, frequencies.size - 1)
            rolloff = float(frequencies[rolloff_index])
            bandwidth = float(
                math.sqrt(float(np.sum(((frequencies - centroid) ** 2) * spectrum)) / magnitude_sum)
            )
        else:
            centroid = 0.0
            rolloff = 0.0
            bandwidth = 0.0

        flatness = float(
            math.exp(float(np.mean(np.log(spectrum + config.eps))))
            / (float(np.mean(spectrum)) + config.eps)
        )
        if magnitude_sum > config.eps:
            normalized_spectrum = spectrum / magnitude_sum
            flux = float(np.sum(np.abs(normalized_spectrum - previous_spectrum)))
        else:
            flux = 0.0
        previous_spectrum = (
            spectrum / magnitude_sum
            if magnitude_sum > config.eps
            else np.zeros_like(spectrum)
        )

        peak_abs = float(np.max(np.abs(raw)))
        if power_sum > config.eps:
            low_ratio = float(np.sum(power[low_mask]) / power_sum)
            high_ratio = float(np.sum(power[high_mask]) / power_sum)
        else:
            low_ratio = 0.0
            high_ratio = 0.0

        all_features[frame_index] = (
            log_energy,
            zcr,
            centroid,
            rolloff,
            bandwidth,
            flatness,
            flux,
            peak_abs,
            low_ratio,
            high_ratio,
        )

    names = FEATURE_NAMES_FULL if config.feature_mode == "full" else FEATURE_NAMES_REDUCED
    indices = [FEATURE_NAMES_FULL.index(name) for name in names]
    features = np.ascontiguousarray(all_features[:, indices], dtype=np.float64)
    return FeatureResult(
        dialog_id=dialog_id,
        features=features,
        frame_indices=np.arange(frame_count, dtype=np.int64),
        sample_starts=starts,
        sample_ends=ends,
        config=config,
        pcm_samples=int(signal.size),
    )


def validate_frame_labels(labels: Sequence[str], *, dialog_id: str) -> None:
    for index, label in enumerate(labels):
        if not isinstance(label, str):
            raise AedError(
                BusinessCode.E_EVENT_TYPE,
                "frame label must be a string",
                dialog_id=dialog_id,
                field="event_type",
                details={"frame_index": index, "event_type": label},
            )
        if label != "background":
            try:
                event_type_id(label)
            except KeyError as exc:
                raise AedError(
                    BusinessCode.E_EVENT_TYPE,
                    f"unknown event type {label!r}",
                    dialog_id=dialog_id,
                    field="event_type",
                    details={"frame_index": index, "event_type": label},
                ) from exc
