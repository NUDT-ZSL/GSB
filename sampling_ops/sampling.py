"""Temperature scaling, inclusive top-p truncation, and local-RNG sampling."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import numpy as np

from .errors import fail
from .generator import make_rng


CDF_EQUALITY_RULE = (
    "random u is drawn with inverse-CDF searchsorted(side='right'); "
    "u equal to an interior cumulative boundary selects the next token"
)
TOP_P_BOUNDARY_RULE = (
    "retain the smallest score-descending prefix whose cumulative probability "
    "is greater than or equal to top_p; equality includes the boundary token"
)


@dataclass
class ScaledCandidates:
    scaled_scores: np.ndarray
    probabilities: np.ndarray
    cumulative_probabilities: np.ndarray
    temperature: float


@dataclass
class TopPResult:
    scaled: ScaledCandidates
    top_p: float
    retained_counts: list[int]
    retained_positions: list[np.ndarray]
    retained_indices: list[np.ndarray]
    retained_probabilities: list[np.ndarray]
    retained_cdfs: list[np.ndarray]
    dropped_counts: list[int]
    cutoff_cumulative: list[float]
    rule: str = TOP_P_BOUNDARY_RULE


@dataclass
class SampleResult:
    samples: np.ndarray
    random_values: np.ndarray
    seed: int
    group_size: int
    top_p_result: TopPResult
    cdf_equality_rule: str = CDF_EQUALITY_RULE
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        retained = []
        for row, (positions, indices) in enumerate(
            zip(self.top_p_result.retained_positions, self.top_p_result.retained_indices)
        ):
            retained.append(
                {
                    "row": row,
                    "candidate_positions": positions.astype(np.int64, copy=False).tolist(),
                    "indices": indices.astype(np.int64, copy=False).tolist(),
                    "retained_count": int(len(positions)),
                    "dropped_count": int(self.top_p_result.dropped_counts[row]),
                    "cutoff_cumulative": float(self.top_p_result.cutoff_cumulative[row]),
                }
            )
        return {
            "seed": int(self.seed),
            "group_size": int(self.group_size),
            "temperature": float(self.top_p_result.scaled.temperature),
            "top_p": float(self.top_p_result.top_p),
            "samples": self.samples.astype(np.int64, copy=False).tolist(),
            "random_values": self.random_values.astype(np.float64, copy=False).tolist(),
            "retained": retained,
            "top_p_boundary_rule": self.top_p_result.rule,
            "cdf_equality_rule": self.cdf_equality_rule,
            "metadata": dict(self.metadata),
        }


def validate_temperature(value: Any, *, case_id: str | None = None) -> float:
    if isinstance(value, bool):
        fail(
            "E_TEMPERATURE_INVALID",
            "temperature must be a finite positive number",
            field="temperature",
            case_id=case_id,
        )
    try:
        temperature = float(value)
    except (TypeError, ValueError, OverflowError):
        fail(
            "E_TEMPERATURE_INVALID",
            "temperature must be a finite positive number",
            field="temperature",
            case_id=case_id,
        )
    if not np.isfinite(temperature) or temperature <= 0:
        fail(
            "E_TEMPERATURE_INVALID",
            "temperature must be finite and greater than zero",
            field="temperature",
            case_id=case_id,
            details={"actual": value},
        )
    return temperature


def validate_top_p(value: Any, *, case_id: str | None = None) -> float:
    if isinstance(value, bool):
        fail(
            "E_TOP_P_INVALID",
            "top_p must be a finite number in (0, 1]",
            field="top_p",
            case_id=case_id,
        )
    try:
        top_p = float(value)
    except (TypeError, ValueError, OverflowError):
        fail(
            "E_TOP_P_INVALID",
            "top_p must be a finite number in (0, 1]",
            field="top_p",
            case_id=case_id,
        )
    if not np.isfinite(top_p) or top_p <= 0 or top_p > 1:
        fail(
            "E_TOP_P_INVALID",
            "top_p must be greater than zero and at most one",
            field="top_p",
            case_id=case_id,
            details={"actual": value},
        )
    return top_p


def validate_seed(value: Any, *, case_id: str | None = None) -> int:
    if value is None or isinstance(value, bool):
        fail(
            "E_SEED_INVALID",
            "seed must be present and must be an integer",
            field="seed",
            case_id=case_id,
            details={"actual": value},
        )
    try:
        seed = int(value)
    except (TypeError, ValueError, OverflowError):
        fail(
            "E_SEED_INVALID",
            "seed must be present and must be an integer",
            field="seed",
            case_id=case_id,
            details={"actual": value},
        )
    if isinstance(value, float) and not value.is_integer():
        fail(
            "E_SEED_INVALID",
            "seed must be an integer",
            field="seed",
            case_id=case_id,
            details={"actual": value},
        )
    return seed


def validate_group_size(value: Any, *, case_id: str | None = None) -> int:
    if isinstance(value, bool):
        fail(
            "E_GROUP_SIZE_INVALID",
            "group_size must be a positive integer",
            field="group_size",
            case_id=case_id,
        )
    try:
        group_size = int(value)
    except (TypeError, ValueError, OverflowError):
        fail(
            "E_GROUP_SIZE_INVALID",
            "group_size must be a positive integer",
            field="group_size",
            case_id=case_id,
        )
    if isinstance(value, (float, np.floating)) and not float(value).is_integer():
        fail(
            "E_GROUP_SIZE_INVALID",
            "group_size must be an integer",
            field="group_size",
            case_id=case_id,
        )
    if group_size <= 0:
        fail(
            "E_GROUP_SIZE_INVALID",
            "group_size must be positive",
            field="group_size",
            case_id=case_id,
        )
    return group_size


def _candidate_scores(scores: Any, *, case_id: str | None) -> np.ndarray:
    array = np.asarray(scores)
    if array.ndim == 1:
        array = array.reshape(1, -1)
    if array.ndim != 2 or array.shape[1] <= 0:
        fail(
            "E_SHAPE_INVALID",
            "candidate scores must be a non-empty 2-D batch",
            field="candidate_scores",
            case_id=case_id,
            details={"shape": list(array.shape)},
        )
    if not np.all(np.isfinite(array)):
        fail(
            "E_LOGITS_NON_FINITE",
            "candidate scores contain a non-finite value",
            field="candidate_scores",
            case_id=case_id,
        )
    return array.astype(np.float64, copy=False)


def scale_candidates(
    candidate_scores: Any,
    temperature: Any,
    *,
    case_id: str | None = None,
) -> ScaledCandidates:
    temperature_value = validate_temperature(temperature, case_id=case_id)
    scores = _candidate_scores(candidate_scores, case_id=case_id)
    scaled = scores / temperature_value
    row_max = np.max(scaled, axis=1, keepdims=True)
    exponentials = np.exp(scaled - row_max)
    probabilities = exponentials / np.sum(exponentials, axis=1, keepdims=True)
    cumulative = np.cumsum(probabilities, axis=1)
    cumulative[:, -1] = 1.0
    return ScaledCandidates(
        scaled_scores=scaled,
        probabilities=probabilities,
        cumulative_probabilities=cumulative,
        temperature=temperature_value,
    )


def truncate_top_p(
    scaled: ScaledCandidates,
    top_p: Any,
    candidate_indices: Any,
    *,
    case_id: str | None = None,
) -> TopPResult:
    top_p_value = validate_top_p(top_p, case_id=case_id)
    indices = np.asarray(candidate_indices)
    if indices.ndim == 1:
        indices = indices.reshape(1, -1)
    if indices.shape != scaled.probabilities.shape:
        fail(
            "E_SHAPE_INVALID",
            "candidate indices and candidate scores must have matching shapes",
            field="candidates",
            case_id=case_id,
            details={"indices_shape": list(indices.shape), "scores_shape": list(scaled.probabilities.shape)},
        )

    retained_counts: list[int] = []
    retained_positions: list[np.ndarray] = []
    retained_indices: list[np.ndarray] = []
    retained_probabilities: list[np.ndarray] = []
    retained_cdfs: list[np.ndarray] = []
    dropped_counts: list[int] = []
    cutoff_cumulative: list[float] = []

    for row in range(indices.shape[0]):
        cdf = scaled.cumulative_probabilities[row]
        if top_p_value >= 1.0:
            count = int(indices.shape[1])
        else:
            count = int(np.searchsorted(cdf, top_p_value, side="left") + 1)
            count = min(max(count, 1), int(indices.shape[1]))
        positions = np.arange(count, dtype=np.int64)
        row_probabilities = scaled.probabilities[row, :count]
        normalized = row_probabilities / np.sum(row_probabilities)
        row_cdf = np.cumsum(normalized)
        row_cdf[-1] = 1.0
        retained_counts.append(count)
        retained_positions.append(positions)
        retained_indices.append(indices[row, :count].astype(np.int64, copy=False))
        retained_probabilities.append(normalized)
        retained_cdfs.append(row_cdf)
        dropped_counts.append(int(indices.shape[1] - count))
        cutoff_cumulative.append(float(cdf[count - 1]))

    return TopPResult(
        scaled=scaled,
        top_p=top_p_value,
        retained_counts=retained_counts,
        retained_positions=retained_positions,
        retained_indices=retained_indices,
        retained_probabilities=retained_probabilities,
        retained_cdfs=retained_cdfs,
        dropped_counts=dropped_counts,
        cutoff_cumulative=cutoff_cumulative,
    )


def draw_samples(
    top_p_result: TopPResult,
    *,
    seed: Any,
    group_size: Any,
    case_id: str | None = None,
) -> SampleResult:
    seed_value = validate_seed(seed, case_id=case_id)
    group_size_value = validate_group_size(group_size, case_id=case_id)
    batch_size = int(top_p_result.scaled.probabilities.shape[0])
    samples = np.empty((batch_size, group_size_value), dtype=np.int64)
    random_values = np.empty((batch_size, group_size_value), dtype=np.float64)
    rng = make_rng(seed_value, stream=0x53414D504C45)
    for row in range(batch_size):
        cdf = top_p_result.retained_cdfs[row]
        count = int(top_p_result.retained_counts[row])
        retained_indices = top_p_result.retained_indices[row]
        for step in range(group_size_value):
            u = float(rng.random())
            position = int(np.searchsorted(cdf, u, side="right"))
            if position >= count:
                position = count - 1
            samples[row, step] = int(retained_indices[position])
            random_values[row, step] = u
    return SampleResult(
        samples=samples,
        random_values=random_values,
        seed=seed_value,
        group_size=group_size_value,
        top_p_result=top_p_result,
        metadata={
            "rng": "numpy.Generator(PCG64(SeedSequence(...)))",
            "global_rng_used": False,
            "draw_order": "row-major then sampling step",
        },
    )


def sample(
    candidate_scores: Any,
    candidate_indices: Any = None,
    *,
    temperature: Any,
    top_p: Any,
    seed: Any,
    group_size: Any = 1,
    case_id: str | None = None,
) -> SampleResult:
    """Convenience wrapper for the full temperature -> top-p -> draw chain."""

    if candidate_indices is None:
        if hasattr(candidate_scores, "scores") and hasattr(candidate_scores, "indices"):
            candidate_indices = candidate_scores.indices
            candidate_scores = candidate_scores.scores
        elif (
            isinstance(candidate_scores, dict)
            and "scores" in candidate_scores
            and "indices" in candidate_scores
        ):
            candidate_indices = candidate_scores["indices"]
            candidate_scores = candidate_scores["scores"]
        else:
            fail(
                "E_SHAPE_INVALID",
                "candidate_indices is required unless candidate_scores is a TopK result",
                field="candidate_indices",
                case_id=case_id,
            )
    scaled = scale_candidates(candidate_scores, temperature, case_id=case_id)
    truncated = truncate_top_p(scaled, top_p, candidate_indices, case_id=case_id)
    return draw_samples(truncated, seed=seed, group_size=group_size, case_id=case_id)

