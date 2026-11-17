"""Deterministic segment-level emotion scoring with two real model paths."""

from __future__ import annotations

import math
from typing import Iterable

import numpy as np

from .constants import CLASSIFIERS, COARSE_EMOTIONS, FINE_EMOTIONS
from .errors import BusinessError
from .model import centroid_parameters, logistic_parameters
from .util import canonicalize, stable_hash


def _softmax(logits: np.ndarray) -> np.ndarray:
    shifted = logits - np.max(logits)
    exps = np.exp(shifted)
    return exps / np.sum(exps)


def _ppm_scores(probabilities: np.ndarray) -> tuple[list[int], float]:
    ppm = [int(math.floor(float(prob) * 1_000_000.0 + 0.5)) for prob in probabilities]
    residual = 1_000_000 - sum(ppm)
    if residual:
        # Give the rounding residual to the current largest value; list order
        # breaks a tie using the category names' dictionary order upstream.
        best = max(range(len(ppm)), key=lambda index: (ppm[index], -index))
        ppm[best] += residual
    return ppm, sum(ppm) / 1_000_000.0


def _stable_scores(categories: tuple[str, ...], probabilities: np.ndarray) -> dict[str, float]:
    ppm, _ = _ppm_scores(probabilities)
    return {category: index / 1_000_000.0 for category, index in zip(categories, ppm)}


def choose_emotion(scores: dict[str, float]) -> str:
    """Dictionary-order tie break for category names."""
    return sorted(scores.items(), key=lambda item: (-item[1], item[0]))[0][0]


def score_segment(
    feature_artifact: dict,
    *,
    granularity: str,
    classifier: str,
    boundary_margin: float,
    reference_precision: float,
    injection_id: str = "",
) -> dict:
    if granularity not in {"coarse", "fine"}:
        raise BusinessError(
            "E_INVALID_GRANULARITY",
            "scores",
            f"unsupported emotion granularity: {granularity}",
            filename=feature_artifact.get("filename", ""),
            exception_type="InvalidGranularityError",
            injection_id=injection_id,
        )
    if classifier not in CLASSIFIERS:
        raise BusinessError(
            "E_INVALID_CONFIG",
            "scores",
            f"unsupported classifier: {classifier}",
            filename=feature_artifact.get("filename", ""),
            exception_type="InvalidClassifierError",
            injection_id=injection_id,
        )
    names = tuple(feature_artifact["effective_features"])
    vector = np.asarray(feature_artifact["segment_feature_vector"], dtype=np.float64)
    if names != tuple(sorted(names, key=lambda item: names.index(item))):
        raise ValueError("feature ordering is not internally stable")
    from .model import FEATURE_MEANS, FEATURE_SCALES, _feature_indices

    indices = _feature_indices(names)
    normalized = (vector - FEATURE_MEANS[indices]) / FEATURE_SCALES[indices]
    if classifier == "nearest_centroid":
        params = centroid_parameters(granularity, names)
        categories = params["categories"]
        centroids = params["centroids"]
        weights = params["feature_weights"]
        differences = normalized[None, :] - centroids
        distances = np.sum(differences * differences * weights[None, :], axis=1) / float(np.sum(weights))
        logits = -float(params["temperature"]) * distances
        probabilities = _softmax(logits)
        parameter_digest = stable_hash(
            {
                "path": params["path"],
                "granularity": granularity,
                "features": names,
                "centroids": centroids.tolist(),
                "weights": weights.tolist(),
                "temperature": params["temperature"],
            }
        )
        path = "weighted_centroid_softmax"
        model_detail = {"average_centroid_distance": float(np.mean(distances))}
    elif classifier == "weighted_logistic_regression":
        params = logistic_parameters(granularity, names)
        categories = params["categories"]
        logits = params["weights"] @ normalized + params["intercept"]
        probabilities = _softmax(logits)
        parameter_digest = params["parameters_digest"]
        path = "balanced_weighted_logistic_softmax"
        model_detail = {"max_abs_logit": float(np.max(np.abs(logits)))}
    else:  # pragma: no cover - guarded above
        raise AssertionError(classifier)

    scores = _stable_scores(categories, probabilities)
    ranked = sorted(scores.items(), key=lambda item: (-item[1], item[0]))
    top_category = choose_emotion(scores)
    margin = round(ranked[0][1] - ranked[1][1], 6) if len(ranked) > 1 else 1.0
    boundary = margin <= boundary_margin
    result = {
        "filename": feature_artifact["filename"],
        "granularity": granularity,
        "classifier": classifier,
        "classifier_path": path,
        "score_path_changed": classifier,
        "classifier_parameters_digest": parameter_digest,
        "categories": list(categories),
        "scores": scores,
        "predicted_emotion": top_category,
        "boundary": boundary,
        "score_margin": margin,
        "reference_precision": reference_precision,
        "ranked_scores": [
            {"category": category, "score": score, "rank": rank + 1}
            for rank, (category, score) in enumerate(ranked)
        ],
        "model_detail": model_detail,
    }
    return canonicalize(result)
