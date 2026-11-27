"""Deterministic scalar-token featureization shared by all format versions."""

from __future__ import annotations

import math
from typing import Any

import numpy as np


def build_token_features(
    token_values: np.ndarray,
    masks: np.ndarray,
    config: dict[str, Any],
) -> np.ndarray:
    """Map scalar normalized token strengths to fixed-dimensional gate inputs.

    The embedding contains content features of the normalized strength plus
    shared positional features. It has no learned or random parameters, so the
    same material segment at the same token index always maps to the same
    feature vector in every format version.
    """

    values = np.asarray(token_values, dtype=np.float64)
    masks = np.asarray(masks, dtype=bool)
    if values.ndim != 2:
        raise ValueError("token_values must have shape [samples, token_length]")
    if values.shape != masks.shape:
        raise ValueError("token_values and masks must have the same shape")
    feature_dim = int(config["model"]["token_feature_dim"])
    sample_count, token_length = values.shape
    features = np.zeros((sample_count, token_length, feature_dim), dtype=np.float64)
    position = np.linspace(0.0, 1.0, token_length, dtype=np.float64)[None, :]

    content_features = [
        values,
        np.abs(values),
        values * values,
        values * np.abs(values),
    ]
    for feature_index, content in enumerate(content_features[:feature_dim]):
        features[:, :, feature_index] = content

    remaining = feature_dim - min(4, feature_dim)
    frequency_index = 1
    feature_index = min(4, feature_dim)
    while remaining > 0:
        angle = math.pi * float(frequency_index) * position
        features[:, :, feature_index] = np.sin(angle)
        remaining -= 1
        feature_index += 1
        if remaining <= 0:
            break
        features[:, :, feature_index] = np.cos(angle)
        remaining -= 1
        feature_index += 1
        frequency_index += 1
    features[~masks] = 0.0
    return features
