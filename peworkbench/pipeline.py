"""Per-(scheme, sample) computation for the encode/score/track stages."""

import numpy as np

from . import (
    E_BASE_ZERO,
    E_NONFINITE_FEATURE,
    E_POS_OUT_OF_RANGE,
    E_SCALE_INVALID,
)
from .schemes import precision_dtype

TRACK_STAGE_ORDER = ("position_vector", "score", "attention_weight")


class FaultError(Exception):
    def __init__(self, code, message):
        super().__init__(message)
        self.code = code


def validate_inputs(sample, config, base, scale):
    features = [sample["features"]["query"]] + list(sample["features"]["keys"])
    values = np.asarray(features, dtype=np.float64)
    if not np.all(np.isfinite(values)):
        raise FaultError(E_NONFINITE_FEATURE,
                         "non-finite dimension value in sample features")
    target = config["extrap_target_len"]
    positions = [sample["query_pos"]] + list(sample["key_positions"])
    if any(int(p) < 0 or int(p) >= target for p in positions):
        raise FaultError(E_POS_OUT_OF_RANGE,
                         "position outside [0, extrap_target_len)")
    if not (float(base) > 0.0):
        raise FaultError(E_BASE_ZERO, "base frequency must be positive")
    if not (float(scale) > 0.0):
        raise FaultError(E_SCALE_INVALID, "scale factor must be positive")


def compute_encode(scheme, sample, config, base, scale):
    validate_inputs(sample, config, base, scale)
    positions = [int(sample["query_pos"])] + [int(p) for p in sample["key_positions"]]
    dim = config["dim"]
    precision = config["precision"]
    vectors = scheme.position_vectors(positions, dim, base, scale, precision)
    reference = scheme.position_vectors(positions, dim, base, scale, "fp64")
    return {
        "positions": positions,
        "pe": np.asarray(vectors, dtype=np.float64).tolist(),
        "pe_ref": np.asarray(reference, dtype=np.float64).tolist(),
    }


def compute_score(scheme, sample, config, encode_result):
    dtype = precision_dtype(config["precision"])
    query = np.asarray(sample["features"]["query"], dtype=np.float64)
    keys = np.asarray(sample["features"]["keys"], dtype=np.float64)
    pe = np.asarray(encode_result["pe"], dtype=np.float64)
    pe_ref = np.asarray(encode_result["pe_ref"], dtype=np.float64)

    q_prec = (query + pe[0]).astype(dtype)
    k_prec = (keys + pe[1:]).astype(dtype)
    scores = np.asarray(k_prec @ q_prec, dtype=np.float64)

    q_ref = query + pe_ref[0]
    k_ref = keys + pe_ref[1:]
    scores_ref = np.asarray(k_ref @ q_ref, dtype=np.float64)

    qpos = int(sample["query_pos"])
    distances = [abs(qpos - int(k)) for k in sample["key_positions"]]
    in_range = [bool(d < config["train_ctx_len"]) for d in distances]
    return {
        "scores": scores.tolist(),
        "scores_ref": scores_ref.tolist(),
        "distances": distances,
        "in_range": in_range,
    }


def _softmax(values):
    arr = np.asarray(values, dtype=np.float64)
    shifted = np.exp(arr - np.max(arr))
    return shifted / np.sum(shifted)


def compute_track(scheme, sample, config, encode_result, score_result):
    tolerance = config["tolerance"]
    train_len = config["train_ctx_len"]
    positions = encode_result["positions"]
    pe = np.asarray(encode_result["pe"], dtype=np.float64)
    pe_ref = np.asarray(encode_result["pe_ref"], dtype=np.float64)
    scores = np.asarray(score_result["scores"], dtype=np.float64)
    scores_ref = np.asarray(score_result["scores_ref"], dtype=np.float64)
    in_range = score_result["in_range"]
    distances = score_result["distances"]

    extrap_rows = [i for i, p in enumerate(positions) if p >= train_len]
    extrap_keys = [i for i, flag in enumerate(in_range) if not flag]

    dev_pe = 0.0
    for row in extrap_rows:
        dev_pe = max(dev_pe, float(np.max(np.abs(pe[row] - pe_ref[row]))))

    dev_score = 0.0
    for idx in extrap_keys:
        dev_score = max(dev_score, float(abs(scores[idx] - scores_ref[idx])))

    dev_weight = 0.0
    if extrap_keys:
        weights = _softmax(scores)
        weights_ref = _softmax(scores_ref)
        for idx in extrap_keys:
            dev_weight = max(dev_weight, float(abs(weights[idx] - weights_ref[idx])))

    deviations = {
        "position_vector": dev_pe,
        "score": dev_score,
        "attention_weight": dev_weight,
    }
    dominant = None
    for stage_name in TRACK_STAGE_ORDER:
        if deviations[stage_name] > tolerance:
            dominant = stage_name
            break

    return {
        "sample_id": sample["sample_id"],
        "scheme": scheme.name,
        "position": int(sample["query_pos"]),
        "max_rel_distance": max((distances[i] for i in extrap_keys), default=0),
        "has_extrapolation": bool(extrap_keys),
        "deviations": deviations,
        "exceeded": dominant is not None,
        "dominant_stage": dominant,
    }
