"""Deterministic segment-level valence/arousal regression.

Regressors are fit on the enrollment (leading speech) frames whose labels are
known, then applied to every contiguous same-speaker segment.

* fine path: ridge regression via a fixed-iteration deterministic coordinate
  descent (no random initialisation);
* approximate path: closed-form univariate ridge on the log-energy feature,
  computable from streaming sufficient statistics.

Confidence intervals are deterministic normal intervals (fixed z quantile);
dialogue aggregation is duration-weighted with an explicitly recorded basis.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np

from .errors import BusinessError, ErrorCode

RIDGE_LAMBDA = 1.0
COORD_STEPS = 60


@dataclass
class SegmentScore:
    segment_index: int
    speaker_id: int
    start_frame: int
    end_frame: int
    duration_frames: int
    valence: float
    arousal: float
    valence_ci: list
    arousal_ci: list


def _design_matrix(features: np.ndarray) -> np.ndarray:
    ones = np.ones((features.shape[0], 1), dtype=np.float64)
    return np.concatenate([ones, features], axis=1)


def _fit_ridge_coordinate(
    x: np.ndarray, y: np.ndarray, iterations: int, term_counter: list,
    term_budget: int, dialog_id: str | None,
) -> np.ndarray:
    n, p = x.shape
    term_counter[0] += n * p
    if term_counter[0] > term_budget:
        raise BusinessError(
            ErrorCode.E_RESOURCE_LIMIT,
            "regression term budget exceeded in fine path fit",
            dialog_id=dialog_id,
            field="max_regression_terms",
            details={"terms": term_counter[0], "budget": term_budget},
        )
    col_norm = np.einsum("ij,ij->j", x, x) + RIDGE_LAMBDA
    beta = np.zeros(p, dtype=np.float64)
    residual = y.copy()
    for _ in range(iterations):
        for j in range(p):
            rho = float(x[:, j] @ residual) + col_norm[j] * beta[j]
            new_beta = rho / col_norm[j]
            residual += (beta[j] - new_beta) * x[:, j]
            beta[j] = new_beta
    return beta


def _fit_ridge_closed(
    x: np.ndarray, y: np.ndarray, term_counter: list,
    term_budget: int, dialog_id: str | None,
) -> np.ndarray:
    n = x.shape[0]
    term_counter[0] += n
    if term_counter[0] > term_budget:
        raise BusinessError(
            ErrorCode.E_RESOURCE_LIMIT,
            "regression term budget exceeded in approximate path fit",
            dialog_id=dialog_id,
            field="max_regression_terms",
            details={"terms": term_counter[0], "budget": term_budget},
        )
    x_mean = float(x.mean())
    y_mean = float(y.mean())
    var = float(np.dot(x - x_mean, x - x_mean)) + RIDGE_LAMBDA
    cov = float(np.dot(x - x_mean, y - y_mean))
    slope = cov / var
    intercept = y_mean - slope * x_mean
    return np.array([intercept, slope], dtype=np.float64)


def fit_regressors(
    features: np.ndarray,
    labels: np.ndarray,
    is_speech: np.ndarray,
    gt_valence: np.ndarray,
    gt_arousal: np.ndarray,
    path: str,
    term_budget: int,
    iterations: int,
    dialog_id: str | None = None,
):
    """Fit one valence and one arousal regressor on labeled enrollment frames.

    Ground-truth emotion targets are used only at this training boundary,
    mirroring a registered labeled sample; inference below uses acoustics only.
    """
    term_counter = [0]
    train_mask = is_speech
    if path == "fine":
        x = _design_matrix(features[train_mask])
        fit = lambda y: _fit_ridge_coordinate(
            x, y, iterations, term_counter, term_budget, dialog_id
        )
        predict = lambda beta, feats: _design_matrix(feats) @ beta
    else:
        x_train = features[train_mask, 1]  # log-energy is index 1
        fit_closed = lambda y: _fit_ridge_closed(
            x_train, y, term_counter, term_budget, dialog_id
        )
        beta_v = fit_closed(gt_valence[train_mask])
        beta_a = fit_closed(gt_arousal[train_mask])

        def predict(beta, feats):
            return beta[0] + beta[1] * feats[:, 1]

        return beta_v, beta_a, predict, term_counter[0]

    beta_v = fit(gt_valence[train_mask])
    beta_a = fit(gt_arousal[train_mask])
    return beta_v, beta_a, predict, term_counter[0]


def _clip(values: np.ndarray) -> np.ndarray:
    return np.clip(values, -1.0, 1.0)


def score_segments(
    features: np.ndarray,
    diarization,
    gt_valence: np.ndarray,
    gt_arousal: np.ndarray,
    path: str,
    confidence_z: float,
    term_budget: int,
    iterations: int,
    dialog_id: str | None = None,
):
    beta_v, beta_a, predict, terms_used = fit_regressors(
        features,
        diarization.labels,
        diarization.is_speech,
        gt_valence,
        gt_arousal,
        path,
        term_budget,
        iterations,
        dialog_id,
    )
    scores: list[SegmentScore] = []
    for seg_index, seg in enumerate(diarization.segments):
        start, end = seg["start_frame"], seg["end_frame"]
        feats = features[start:end]
        raw_v = _clip(predict(beta_v, feats))
        raw_a = _clip(predict(beta_a, feats))
        valence = float(raw_v.mean())
        arousal = float(raw_a.mean())
        # Deterministic residual spread against the enrollment-trained model;
        # minimum width prevents zero-width intervals on constant segments.
        se_v = max(float(raw_v.std(ddof=0)), 1e-3)
        se_a = max(float(raw_a.std(ddof=0)), 1e-3)
        half_v = confidence_z * se_v / np.sqrt(max(end - start, 1))
        half_a = confidence_z * se_a / np.sqrt(max(end - start, 1))
        v_ci = [float(np.clip(valence - half_v, -1.0, 1.0)),
                float(np.clip(valence + half_v, -1.0, 1.0))]
        a_ci = [float(np.clip(arousal - half_a, -1.0, 1.0)),
                float(np.clip(arousal + half_a, -1.0, 1.0))]
        if v_ci[0] > v_ci[1] or a_ci[0] > a_ci[1]:
            raise BusinessError(
                ErrorCode.E_INTERVAL_INVALID,
                "confidence interval lower bound exceeds upper bound",
                dialog_id=dialog_id,
                details={"valence_ci": v_ci, "arousal_ci": a_ci},
            )
        scores.append(
            SegmentScore(
                segment_index=seg_index,
                speaker_id=int(seg["speaker_id"]),
                start_frame=int(start),
                end_frame=int(end),
                duration_frames=int(end - start),
                valence=valence,
                arousal=arousal,
                valence_ci=v_ci,
                arousal_ci=a_ci,
            )
        )
    return scores, terms_used


def aggregate_dialogue(scores: list[SegmentScore]) -> dict:
    """Duration-weighted dialogue aggregation with recorded weight basis."""
    weights = np.array(
        [s.duration_frames for s in scores], dtype=np.float64
    )
    total = float(weights.sum())
    if total <= 0:
        raise BusinessError(
            ErrorCode.E_INCONSISTENT,
            "cannot aggregate dialogue with zero speech frames",
            details={"category": "empty_aggregation"},
        )
    norm = weights / total

    def weighted(attr: str) -> float:
        return float(sum(norm[i] * getattr(s, attr) for i, s in enumerate(scores)))

    valence = weighted("valence")
    arousal = weighted("arousal")
    v_low = float(sum(norm[i] * s.valence_ci[0] for i, s in enumerate(scores)))
    v_high = float(sum(norm[i] * s.valence_ci[1] for i, s in enumerate(scores)))
    a_low = float(sum(norm[i] * s.arousal_ci[0] for i, s in enumerate(scores)))
    a_high = float(sum(norm[i] * s.arousal_ci[1] for i, s in enumerate(scores)))
    if v_low > v_high or a_low > a_high:
        raise BusinessError(
            ErrorCode.E_INTERVAL_INVALID,
            "aggregated interval order is illegal",
        )
    return {
        "valence": valence,
        "arousal": arousal,
        "valence_ci": [v_low, v_high],
        "arousal_ci": [a_low, a_high],
        "weight_basis": "segment_duration_frames",
        "weights": [float(w) for w in norm],
        "total_speech_frames": int(total),
    }
