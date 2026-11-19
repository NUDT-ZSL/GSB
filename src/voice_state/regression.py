"""Deterministic segment-level affect regression with confidence intervals."""

from __future__ import annotations

import math

import numpy as np

from .canonical import checksum_value, round_float
from .config import AnalysisConfig
from .errors import BusinessError, E_INCONSISTENT
from .features import spectral_descriptors


MODEL_KIND = "deterministic_fixed_ridge_linear_v1"
MODEL_COEFFICIENTS = {
    "feature_centers": {
        "log_energy": 8.80,
        "zcr": 0.104,
        "centroid": 0.089,
        "flatness": 0.007,
        "band_0": 0.234,
        "band_1": 0.745,
        "band_2": 0.019,
        "band_3": 0.001,
    },
    "feature_scales": {
        "log_energy": 0.30,
        "zcr": 0.020,
        "centroid": 0.020,
        "flatness": 0.003,
        "band_0": 0.060,
        "band_1": 0.040,
        "band_2": 0.020,
        "band_3": 0.002,
    },
    "valence": {
        "bias": 0.0,
        "log_energy": 0.03,
        "zcr": -0.35,
        "centroid": 1.10,
        "flatness": 0.20,
        "band_0": -0.80,
        "band_1": 0.35,
        "band_2": 0.55,
        "band_3": 0.20,
    },
    "arousal": {
        "bias": 0.0,
        "log_energy": 0.65,
        "zcr": 0.25,
        "centroid": 0.35,
        "flatness": 0.10,
        "band_0": 0.20,
        "band_1": 0.15,
        "band_2": -0.10,
        "band_3": 0.05,
    },
}
MODEL_CHECKSUM = checksum_value(MODEL_COEFFICIENTS)


def regress_segments(
    dialog_id: str,
    samples: np.ndarray,
    segmentation: dict,
    config: AnalysisConfig,
) -> dict:
    descriptors = spectral_descriptors(samples, config)
    num_frames = int(segmentation.get("num_frames", 0))
    if descriptors.shape[0] != num_frames:
        raise BusinessError(
            E_INCONSISTENT,
            "Frame count changed between segmentation and regression",
            {
                "dialog_id": dialog_id,
                "segmentation_frames": num_frames,
                "regression_frames": int(descriptors.shape[0]),
            },
        )
    segments_out: list[dict] = []
    for segment in segmentation["segments"]:
        start = int(segment["start_frame"])
        end = int(segment["end_frame"])
        if not 0 <= start <= end < descriptors.shape[0]:
            raise BusinessError(
                E_INCONSISTENT,
                "Segment bounds are outside the dialog",
                {"dialog_id": dialog_id, "start_frame": start, "end_frame": end},
            )
        window = descriptors[start : end + 1]
        active = window[window[:, 0] > 0.05]
        if active.shape[0] == 0:
            active = window
        valence_frame, arousal_frame = _predict_frames(active)
        valence = _summarize(valence_frame, config)
        arousal = _summarize(arousal_frame, config)
        segments_out.append(
            {
                **segment,
                "valence": valence["mean"],
                "arousal": arousal["mean"],
                "valence_ci95": [valence["lower"], valence["upper"]],
                "arousal_ci95": [arousal["lower"], arousal["upper"]],
                "observed_frames": int(active.shape[0]),
            }
        )
    return {
        "dialog_id": dialog_id,
        "num_frames": num_frames,
        "segments": segments_out,
        "model": {
            "kind": MODEL_KIND,
            "coefficients_checksum": MODEL_CHECKSUM,
            "confidence_method": "z*sqrt(model_sigma^2 + sample_variance/n)",
            "confidence_z": config.confidence_z,
            "score_places": config.score_places,
        },
    }


def _predict_frames(descriptors: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    cv = MODEL_COEFFICIENTS["valence"]
    ca = MODEL_COEFFICIENTS["arousal"]
    names = (
        "log_energy",
        "zcr",
        "centroid",
        "flatness",
        "band_0",
        "band_1",
        "band_2",
        "band_3",
    )
    valence_linear = np.full(descriptors.shape[0], cv["bias"], dtype=np.float64)
    arousal_linear = np.full(descriptors.shape[0], ca["bias"], dtype=np.float64)
    for column, name in enumerate(names):
        center = MODEL_COEFFICIENTS["feature_centers"][name]
        scale = MODEL_COEFFICIENTS["feature_scales"][name]
        standardized = (descriptors[:, column] - center) / scale
        valence_linear += cv[name] * standardized
        arousal_linear += ca[name] * standardized
    return np.tanh(valence_linear), np.tanh(arousal_linear)


def _summarize(predictions: np.ndarray, config: AnalysisConfig) -> dict:
    mean = float(np.mean(predictions))
    count = int(predictions.size)
    variance = float(np.var(predictions, ddof=0)) if count else 0.0
    uncertainty = math.sqrt(config.model_sigma**2 + variance / max(1, count))
    margin = config.confidence_z * uncertainty
    return {
        "mean": round_float(mean, config.score_places),
        "lower": round_float(max(-1.0, mean - margin), config.score_places),
        "upper": round_float(min(1.0, mean + margin), config.score_places),
    }

