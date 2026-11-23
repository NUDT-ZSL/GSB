from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any, Callable

import numpy as np

from .canonical import sha256_obj
from .config import WorkbenchConfig
from .convergence import ConvergenceDecision, decide_convergence
from .precision import PrecisionOps


LOG_2PI = math.log(2.0 * math.pi)


@dataclass
class GMMModel:
    weights: np.ndarray
    means: np.ndarray
    covariances: np.ndarray
    covariance_type: str
    payload_hash: str

    def to_payload(self) -> dict[str, Any]:
        return {
            "kind": "gmm",
            "weights": self.weights.tolist(),
            "means": self.means.tolist(),
            "covariances": self.covariances.tolist(),
            "covariance_type": self.covariance_type,
            "payload_hash": self.payload_hash,
        }

    @classmethod
    def from_payload(cls, payload: dict[str, Any]) -> "GMMModel":
        return cls(
            weights=np.asarray(payload["weights"], dtype=np.float64),
            means=np.asarray(payload["means"], dtype=np.float64),
            covariances=np.asarray(payload["covariances"], dtype=np.float64),
            covariance_type=str(payload["covariance_type"]),
            payload_hash=str(payload["payload_hash"]),
        )


@dataclass
class EStepChunk:
    responsibilities: np.ndarray
    log_probabilities: np.ndarray
    log_norm: np.ndarray
    density: np.ndarray
    log_likelihood: float
    component_evaluations: int


@dataclass
class GMMFit:
    model: GMMModel
    responsibilities: np.ndarray
    log_probabilities: np.ndarray
    log_norm: np.ndarray
    densities: np.ndarray
    log_likelihood: float
    trace: list[dict[str, Any]]
    protected_rounds: list[dict[str, Any]]
    skipped_records: list[dict[str, Any]]
    convergence: dict[str, Any]
    chunk_summaries: list[dict[str, Any]]


def _model_hash(
    weights: np.ndarray,
    means: np.ndarray,
    covariances: np.ndarray,
    covariance_type: str,
) -> str:
    return sha256_obj(
        {
            "kind": "gmm",
            "weights": weights.tolist(),
            "means": means.tolist(),
            "covariances": covariances.tolist(),
            "covariance_type": covariance_type,
        }
    )


def build_model(
    weights: np.ndarray,
    means: np.ndarray,
    covariances: np.ndarray,
    covariance_type: str,
    ops: PrecisionOps,
) -> GMMModel:
    weights = ops.cast(np.asarray(weights, dtype=np.float64))
    means = ops.cast(np.asarray(means, dtype=np.float64))
    covariances = ops.cast(np.asarray(covariances, dtype=np.float64))
    digest = _model_hash(weights, means, covariances, covariance_type)
    return GMMModel(
        weights=weights,
        means=means,
        covariances=covariances,
        covariance_type=covariance_type,
        payload_hash=digest,
    )


def initialize_gmm(
    values: np.ndarray,
    config: WorkbenchConfig,
    ops: PrecisionOps,
) -> GMMModel:
    x = ops.cast(np.asarray(values, dtype=np.float64))
    n_samples, dimensions = x.shape
    k = config.n_components
    if k > n_samples:
        from .errors import E_CONFIG_INVALID, BusinessError

        raise BusinessError(
            code=E_CONFIG_INVALID,
            message="n_components cannot exceed the number of samples",
            details={
                "field": "n_components",
                "n_components": k,
                "n_samples": n_samples,
            },
        )
    rng = np.random.default_rng(config.seed)
    indices = np.sort(rng.choice(n_samples, size=k, replace=False))
    means = ops.cast(x[indices].copy())
    jitter = rng.normal(0.0, 0.01, size=means.shape)
    means = ops.add(means, jitter)

    global_mean = ops.div(ops.sum(x, axis=0), float(n_samples))
    centered = ops.sub(x, global_mean)
    global_variance = ops.div(
        ops.sum(ops.mul(centered, centered), axis=0), float(n_samples)
    )
    floor = float(config.covariance_floor)
    if config.covariance_type == "full":
        covariance = np.zeros((dimensions, dimensions), dtype=np.float64)
        for row in centered:
            covariance = ops.add(
                covariance,
                ops.mul(row.reshape(-1, 1), row.reshape(1, -1)),
            )
        covariance = ops.div(covariance, float(n_samples))
        covariance = ops.add(
            covariance, np.eye(dimensions, dtype=np.float64) * floor
        )
        covariances = np.repeat(covariance[None, :, :], k, axis=0)
    elif config.covariance_type == "diag":
        diagonal = ops.add(global_variance, floor)
        covariances = np.repeat(diagonal[None, :], k, axis=0)
    else:
        scalar = float(np.mean(np.asarray(global_variance, dtype=np.float64)))
        covariances = np.full((k,), max(scalar + floor, floor), dtype=np.float64)
    weights = np.full((k,), 1.0 / float(k), dtype=np.float64)
    return build_model(weights, means, covariances, config.covariance_type, ops)


def _safe_cholesky(
    covariance: np.ndarray,
    ops: PrecisionOps,
    floor: float,
) -> np.ndarray:
    dtype = np.float32 if ops.mode == "float32" else np.float64
    matrix = np.asarray(covariance, dtype=dtype).copy()
    if ops.mode == "fixed":
        matrix = ops.cast(matrix).astype(np.float64)
    jitter = float(floor)
    for attempt in range(8):
        try:
            return np.linalg.cholesky(matrix)
        except np.linalg.LinAlgError:
            matrix = matrix + np.eye(matrix.shape[0], dtype=dtype) * jitter
            jitter = max(jitter * 10.0, np.finfo(dtype).eps)
    raise ValueError("covariance matrix is not positive definite")


def component_log_probabilities(
    values: np.ndarray,
    model: GMMModel,
    ops: PrecisionOps,
    covariance_floor: float,
) -> np.ndarray:
    x = ops.cast(np.asarray(values, dtype=np.float64))
    means = ops.cast(model.means)
    differences = ops.sub(x[:, None, :], means[None, :, :])
    dimensions = x.shape[1]
    k = means.shape[0]
    result = np.empty((x.shape[0], k), dtype=np.float64)
    for component in range(k):
        diff = differences[:, component, :]
        if model.covariance_type == "full":
            covariance = ops.cast(model.covariances[component])
            chol = _safe_cholesky(covariance, ops, covariance_floor)
            solve_dtype = chol.dtype
            solved = np.linalg.solve(
                chol, np.asarray(diff, dtype=solve_dtype).T
            )
            quadratic = ops.sum(ops.mul(solved.T, solved.T), axis=1)
            log_det = ops.mul(
                ops.sum(ops.log(np.diag(chol).astype(np.float64))),
                2.0,
            )
        elif model.covariance_type == "diag":
            variance = ops.cast(model.covariances[component])
            quadratic = ops.sum(
                ops.div(ops.mul(diff, diff), variance[None, :]), axis=1
            )
            log_det = ops.sum(ops.log(variance))
        else:
            scalar = ops.cast(model.covariances[component])
            quadratic = ops.div(
                ops.sum(ops.mul(diff, diff), axis=1), scalar
            )
            log_det = ops.mul(ops.log(scalar), float(dimensions))
        constant = ops.cast(float(dimensions) * LOG_2PI + float(log_det))
        result[:, component] = ops.mul(
            ops.add(quadratic, constant), -0.5
        )
    return ops.cast(result)


def e_step_chunk(
    values: np.ndarray,
    model: GMMModel,
    config: WorkbenchConfig,
    ops: PrecisionOps,
) -> EStepChunk:
    x = ops.cast(np.asarray(values, dtype=np.float64))
    component_logs = component_log_probabilities(
        x, model, ops, config.covariance_floor
    )
    log_weights = ops.log(ops.cast(model.weights))
    weighted = ops.add(component_logs, log_weights[None, :])
    maxima = np.max(weighted, axis=1)
    shifted = ops.exp(ops.sub(weighted, maxima[:, None]))
    denominators = ops.sum(shifted, axis=1)
    responsibilities = ops.div(shifted, denominators[:, None])
    log_norm = ops.add(maxima, ops.log(denominators))
    density = ops.exp(log_norm)
    log_likelihood = float(ops.sum(log_norm))
    return EStepChunk(
        responsibilities=responsibilities,
        log_probabilities=component_logs,
        log_norm=log_norm,
        density=density,
        log_likelihood=log_likelihood,
        component_evaluations=int(x.shape[0] * model.means.shape[0]),
    )


def m_step(
    values: np.ndarray,
    responsibilities: np.ndarray,
    previous: GMMModel,
    config: WorkbenchConfig,
    ops: PrecisionOps,
    chunk_bounds: list[tuple[int, int]],
    stats_cache: Callable[[str, Any, Callable[[], Any]], Any] | None = None,
) -> GMMModel:
    x = ops.cast(np.asarray(values, dtype=np.float64))
    r = ops.cast(np.asarray(responsibilities, dtype=np.float64))
    n_samples, dimensions = x.shape
    k = previous.means.shape[0]

    count = np.zeros(k, dtype=np.float64)
    weighted_sum = np.zeros((k, dimensions), dtype=np.float64)
    for chunk_number, (start, stop) in enumerate(chunk_bounds):
        chunk_r = r[start:stop]

        def build_counts() -> np.ndarray:
            return ops.sum(chunk_r, axis=0)

        chunk_count = (
            stats_cache(
                f"m_count:{chunk_number}",
                {"start": start, "stop": stop},
                build_counts,
            )
            if stats_cache
            else build_counts()
        )
        count = ops.add(count, chunk_count)

        chunk_weighted = np.zeros((k, dimensions), dtype=np.float64)
        for component in range(k):
            term = ops.mul(
                chunk_r[:, component : component + 1], x[start:stop]
            )
            chunk_weighted[component] = ops.sum(term, axis=0)
        weighted_sum = ops.add(weighted_sum, chunk_weighted)

    effective = np.asarray(count, dtype=np.float64) > max(
        ops.unit * 0.5, np.finfo(np.float64).tiny
    )
    means = np.array(previous.means, dtype=np.float64, copy=True)
    for component in range(k):
        if effective[component] and count[component] > 0.0:
            means[component] = ops.div(
                weighted_sum[component], count[component]
            )
    means = ops.cast(means)

    covariance_accumulator: np.ndarray
    if config.covariance_type == "full":
        covariance_accumulator = np.zeros(
            (k, dimensions, dimensions), dtype=np.float64
        )
    elif config.covariance_type == "diag":
        covariance_accumulator = np.zeros((k, dimensions), dtype=np.float64)
    else:
        covariance_accumulator = np.zeros((k,), dtype=np.float64)

    for chunk_number, (start, stop) in enumerate(chunk_bounds):
        chunk_r = r[start:stop]
        chunk_x = x[start:stop]
        chunk_diffs = ops.sub(chunk_x[:, None, :], means[None, :, :])

        def build_covariance() -> np.ndarray:
            if config.covariance_type == "full":
                out = np.zeros(
                    (k, dimensions, dimensions), dtype=np.float64
                )
                for component in range(k):
                    weighted_diff = ops.mul(
                        chunk_diffs[:, component, :],
                        chunk_r[:, component : component + 1],
                    )
                    outer = ops.mul(
                        weighted_diff[:, :, None], chunk_diffs[:, component, None, :]
                    )
                    out[component] = ops.sum(outer, axis=0)
                return out
            if config.covariance_type == "diag":
                out = np.zeros((k, dimensions), dtype=np.float64)
                for component in range(k):
                    weighted_square = ops.mul(
                        ops.mul(
                            chunk_diffs[:, component, :],
                            chunk_diffs[:, component, :],
                        ),
                        chunk_r[:, component : component + 1],
                    )
                    out[component] = ops.sum(weighted_square, axis=0)
                return out
            out = np.zeros((k,), dtype=np.float64)
            for component in range(k):
                weighted_square = ops.mul(
                    ops.sum(
                        ops.mul(
                            chunk_diffs[:, component, :],
                            chunk_diffs[:, component, :],
                        ),
                        axis=1,
                    ),
                    chunk_r[:, component],
                )
                out[component] = ops.div(
                    ops.sum(weighted_square), float(dimensions)
                )
            return out

        chunk_covariance = (
            stats_cache(
                f"m_cov:{chunk_number}",
                {"start": start, "stop": stop},
                build_covariance,
            )
            if stats_cache
            else build_covariance()
        )
        covariance_accumulator = ops.add(
            covariance_accumulator, chunk_covariance
        )

    if config.covariance_type == "full":
        covariances = np.zeros_like(covariance_accumulator)
        for component in range(k):
            if effective[component] and count[component] > 0.0:
                covariances[component] = ops.div(
                    covariance_accumulator[component], count[component]
                )
            else:
                covariances[component] = previous.covariances[component]
            covariances[component] = ops.add(
                covariances[component],
                np.eye(dimensions, dtype=np.float64) * config.covariance_floor,
            )
    elif config.covariance_type == "diag":
        covariances = np.zeros_like(covariance_accumulator)
        for component in range(k):
            if effective[component] and count[component] > 0.0:
                covariances[component] = ops.div(
                    covariance_accumulator[component], count[component]
                )
            else:
                covariances[component] = previous.covariances[component]
        covariances = ops.add(
            covariances, float(config.covariance_floor)
        )
    else:
        covariances = np.zeros_like(covariance_accumulator)
        for component in range(k):
            if effective[component] and count[component] > 0.0:
                covariances[component] = ops.div(
                    covariance_accumulator[component], count[component]
                )
            else:
                covariances[component] = previous.covariances[component]
        covariances = np.maximum(
            ops.add(covariances, float(config.covariance_floor)),
            float(config.covariance_floor),
        )
    weights = ops.div(count, float(n_samples))
    weight_sum = float(ops.sum(weights))
    if weight_sum > 0.0:
        weights = ops.div(weights, weight_sum)
    return build_model(
        weights, means, covariances, config.covariance_type, ops
    )


def fit_gmm(
    values: np.ndarray,
    config: WorkbenchConfig,
    ops: PrecisionOps,
    *,
    initial_model: GMMModel | None = None,
    chunk_bounds: list[tuple[int, int]] | None = None,
    e_step_callback: (
        Callable[[int, int, np.ndarray, GMMModel], EStepChunk] | None
    ) = None,
    m_step_cache: Callable[[str, Any, Callable[[], Any]], Any] | None = None,
) -> GMMFit:
    x = ops.cast(np.asarray(values, dtype=np.float64))
    n_samples = x.shape[0]
    if chunk_bounds is None:
        chunk_bounds = [
            (start, min(start + config.chunk_size, n_samples))
            for start in range(0, n_samples, config.chunk_size)
        ]
    model = initial_model or initialize_gmm(x, config, ops)
    trace: list[dict[str, Any]] = []
    protected_rounds: list[dict[str, Any]] = []
    skipped_records: list[dict[str, Any]] = []
    chunk_summaries: list[dict[str, Any]] = []
    previous_ll: float | None = None
    last_e_step: EStepChunk | None = None
    converged = False
    final_decision: ConvergenceDecision | None = None
    final_iteration = 0
    final_model = model

    for iteration in range(1, config.max_iter + 1):
        responsibilities_parts: list[np.ndarray] = []
        log_probability_parts: list[np.ndarray] = []
        log_norm_parts: list[np.ndarray] = []
        density_parts: list[np.ndarray] = []
        chunk_ll: list[float] = []
        evaluations = 0
        for chunk_number, (start, stop) in enumerate(chunk_bounds):
            if e_step_callback is not None:
                chunk_result = e_step_callback(
                    iteration, chunk_number, x[start:stop], model
                )
            else:
                chunk_result = e_step_chunk(
                    x[start:stop], model, config, ops
                )
            responsibilities_parts.append(chunk_result.responsibilities)
            log_probability_parts.append(chunk_result.log_probabilities)
            log_norm_parts.append(chunk_result.log_norm)
            density_parts.append(chunk_result.density)
            chunk_ll.append(chunk_result.log_likelihood)
            evaluations += chunk_result.component_evaluations
        responsibilities = ops.cast(np.vstack(responsibilities_parts))
        log_probabilities = ops.cast(np.vstack(log_probability_parts))
        log_norm = ops.cast(np.concatenate(log_norm_parts))
        densities = ops.cast(np.concatenate(density_parts))
        # Explicit reduction: chunk likelihoods are folded in ascending chunk
        # order through the precision-specific accumulator.
        ll_accumulator = np.asarray(0.0, dtype=np.float64)
        for value in chunk_ll:
            ll_accumulator = ops.add(ll_accumulator, value)
        log_likelihood = float(ll_accumulator)
        last_e_step = EStepChunk(
            responsibilities=responsibilities,
            log_probabilities=log_probabilities,
            log_norm=log_norm,
            density=densities,
            log_likelihood=log_likelihood,
            component_evaluations=evaluations,
        )
        chunk_summaries.append(
            {
                "iteration": iteration,
                "chunk_count": len(chunk_bounds),
                "chunk_log_likelihoods": [float(value) for value in chunk_ll],
                "reduction_order": list(range(len(chunk_bounds))),
                "log_likelihood": log_likelihood,
                "component_evaluations": evaluations,
            }
        )
        trace_entry: dict[str, Any] = {
            "iteration": iteration,
            "log_likelihood": log_likelihood,
            "improvement": None,
            "converged": False,
            "model_hash": model.payload_hash,
            "convergence_decision": None,
        }
        if previous_ll is not None:
            improvement = log_likelihood - previous_ll
            decision = decide_convergence(
                iteration=iteration,
                improvement=improvement,
                tol=config.tol,
                min_improvement=config.min_improvement,
                policy=config.convergence_policy,
                rel_band=config.convergence_rel_band,
                abs_band=config.convergence_abs_band,
                subnormal_floor=config.subnormal_floor,
                numerical_epsilon=ops.unit,
            )
            trace_entry["improvement"] = float(improvement)
            trace_entry["converged"] = decision.converged
            trace_entry["convergence_decision"] = decision.to_dict()
            if decision.protected:
                protected_rounds.append(decision.to_dict())
            if decision.converged:
                converged = True
                final_decision = decision
                final_iteration = iteration
                trace.append(trace_entry)
                skipped_records.append(
                    {
                        "iteration": iteration,
                        "record_type": "M_STEP_SKIPPED_AFTER_CONVERGENCE",
                        "reason": decision.reason,
                        "model_hash": model.payload_hash,
                    }
                )
                final_model = model
                break
        trace.append(trace_entry)
        previous_ll = log_likelihood
        final_iteration = iteration
        if iteration < config.max_iter:
            model = m_step(
                x,
                responsibilities,
                model,
                config,
                ops,
                chunk_bounds,
                stats_cache=m_step_cache,
            )
        else:
            final_model = model
            skipped_records.append(
                {
                    "iteration": iteration,
                    "record_type": "MAX_ITER_REACHED",
                    "reason": "iteration_limit",
                    "model_hash": model.payload_hash,
                }
            )
            break
        final_model = model

    if last_e_step is None:  # pragma: no cover - max_iter is positive
        raise RuntimeError("GMM fit produced no E-step")

    # The last E-step is always evaluated under final_model. When max_iter is
    # exhausted, no additional unevaluated M-step remains.
    final_e = last_e_step

    convergence_payload = {
        "converged": converged,
        "reason": (
            final_decision.reason if final_decision is not None else "max_iter"
        ),
        "iteration": final_iteration,
        "final_model_hash": final_model.payload_hash,
        "protected_rounds": protected_rounds,
        "skipped_records": skipped_records,
    }
    return GMMFit(
        model=final_model,
        responsibilities=final_e.responsibilities,
        log_probabilities=final_e.log_probabilities,
        log_norm=final_e.log_norm,
        densities=final_e.density,
        log_likelihood=final_e.log_likelihood,
        trace=trace,
        protected_rounds=protected_rounds,
        skipped_records=skipped_records,
        convergence=convergence_payload,
        chunk_summaries=chunk_summaries,
    )


def responsibility_confidence(responsibilities: np.ndarray) -> dict[str, Any]:
    probabilities = np.asarray(responsibilities, dtype=np.float64)
    probabilities = probabilities / max(float(np.sum(probabilities)), 1e-300)
    positive = probabilities[probabilities > 0.0]
    entropy = (
        float(-np.sum(positive * np.log(positive))) if positive.size else 0.0
    )
    ordered = np.sort(probabilities)[::-1]
    top = float(ordered[0]) if ordered.size else 0.0
    margin = float(ordered[0] - ordered[1]) if ordered.size > 1 else top
    return {
        "top_responsibility": top,
        "top_margin": margin,
        "entropy": entropy,
        "effective_components": float(math.exp(entropy)),
        "component": int(np.argmax(probabilities)) if probabilities.size else None,
    }
