"""Detection configuration normalization and validation."""

from __future__ import annotations

import hashlib
import math
from dataclasses import dataclass
from typing import Any

from .errors import EngineError

SUPPORTED_METHODS = ("isolation_forest", "knn", "mahalanobis")
METHOD_ALIASES = {
    "isolation_forest": "isolation_forest",
    "iforest": "isolation_forest",
    "isolation-forest": "isolation_forest",
    "knn": "knn",
    "k_nearest_neighbors": "knn",
    "mahalanobis": "mahalanobis",
}
METHOD_ORDER = {method: index for index, method in enumerate(SUPPORTED_METHODS)}


@dataclass(frozen=True)
class CandidateSpec:
    candidate_id: str
    method: str
    candidate_index: int
    params: dict[str, Any]
    seed: int


@dataclass(frozen=True)
class EngineConfig:
    run_seed: int
    candidates: tuple[CandidateSpec, ...]
    workers: int
    batch_size: int
    start_method: str
    max_comparisons: int
    max_memory_bytes: int
    priority_order: tuple[str, ...]
    quota_fraction: float
    score_semantics: str = "higher_is_more_anomalous"

    def serializable(self) -> dict[str, Any]:
        return {
            "run_seed": self.run_seed,
            "candidates": [
                {
                    "candidate_id": candidate.candidate_id,
                    "method": candidate.method,
                    "candidate_index": candidate.candidate_index,
                    "params": candidate.params,
                    "seed": candidate.seed,
                }
                for candidate in self.candidates
            ],
            "parallel": {
                "workers": self.workers,
                "batch_size": self.batch_size,
                "batch_size_semantics": "maximum sample rows per algorithm chunk",
                "start_method": self.start_method,
            },
            "budgets": {
                "max_comparisons": self.max_comparisons,
                "max_memory_bytes": self.max_memory_bytes,
            },
            "priority": {"order": list(self.priority_order)},
            "evaluation": {"quota_fraction": self.quota_fraction},
            "score_semantics": self.score_semantics,
        }


def _business_error(code: str, field: str, message: str) -> EngineError:
    return EngineError(code, "config_validation", message, field=field)


def _positive_int(value: Any, field: str, code: str = "E_INVALID_CONFIG") -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise _business_error(code, field, f"{field} must be a positive integer")
    return value


def _positive_float(value: Any, field: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise _business_error("E_INVALID_CONFIG", field, f"{field} must be a positive number")
    converted = float(value)
    if not math.isfinite(converted) or converted <= 0:
        raise _business_error("E_INVALID_CONFIG", field, f"{field} must be a finite positive number")
    return converted


def derive_method_seed(run_seed: int, method: str) -> int:
    payload = f"anomaly-engine-v1:{run_seed}:{method}".encode("utf-8")
    return int.from_bytes(hashlib.sha256(payload).digest()[:8], "big") % (2**32)


def _method_candidates(raw_methods: Any) -> list[tuple[str, int, dict[str, Any]]]:
    if isinstance(raw_methods, dict):
        unknown = [name for name in raw_methods if METHOD_ALIASES.get(name) is None]
        if unknown:
            name = str(unknown[0])
            raise _business_error(
                "E_UNKNOWN_METHOD",
                f"methods.{name}",
                f"unknown detection method {name!r}",
            )
        canonical_names = [METHOD_ALIASES[name] for name in raw_methods]
        if len(set(canonical_names)) != len(canonical_names):
            raise _business_error(
                "E_DUPLICATE_METHOD",
                "methods",
                "the same method cannot be declared through multiple aliases",
            )
        output: list[tuple[str, int, dict[str, Any]]] = []
        for name in sorted(raw_methods, key=lambda item: METHOD_ORDER[METHOD_ALIASES[item]]):
            method = METHOD_ALIASES[name]
            entries = raw_methods[name]
            if entries is None:
                continue
            if not isinstance(entries, list):
                raise _business_error(
                    "E_INVALID_CONFIG",
                    f"methods.{name}",
                    "candidate hyperparameters must be a list",
                )
            for index, params in enumerate(entries):
                if not isinstance(params, dict):
                    raise _business_error(
                        "E_INVALID_CONFIG",
                        f"methods.{name}[{index}]",
                        "candidate hyperparameters must be an object",
                    )
                output.append((method, index, dict(params)))
        return output

    if isinstance(raw_methods, list):
        output = []
        counters = {method: 0 for method in SUPPORTED_METHODS}
        for index, entry in enumerate(raw_methods):
            if not isinstance(entry, dict):
                raise _business_error(
                    "E_INVALID_CONFIG",
                    f"methods[{index}]",
                    "method candidate must be an object",
                )
            raw_name = entry.get("method")
            method = METHOD_ALIASES.get(raw_name) if isinstance(raw_name, str) else None
            if method is None:
                raise _business_error(
                    "E_UNKNOWN_METHOD",
                    f"methods[{index}].method",
                    f"unknown detection method {raw_name!r}",
                )
            params = entry.get("params", {key: value for key, value in entry.items() if key != "method"})
            if not isinstance(params, dict):
                raise _business_error(
                    "E_INVALID_CONFIG",
                    f"methods[{index}].params",
                    "params must be an object",
                )
            candidate_index = counters[method]
            counters[method] += 1
            output.append((method, candidate_index, dict(params)))
        return output

    raise _business_error("E_INVALID_CONFIG", "methods", "methods must be an object or list")


def _normalize_iforest(params: dict[str, Any], n_rows: int, field: str) -> dict[str, Any]:
    allowed = {"n_trees", "sample_size", "max_depth", "seed"}
    unknown = sorted(set(params) - allowed)
    if unknown:
        raise _business_error(
            "E_UNKNOWN_HYPERPARAMETER",
            f"{field}.{unknown[0]}",
            f"unknown isolation_forest hyperparameter {unknown[0]!r}",
        )
    n_trees = _positive_int(params.get("n_trees", 64), f"{field}.n_trees", "E_INVALID_TREES")
    sample_size = _positive_int(
        params.get("sample_size", min(256, n_rows)),
        f"{field}.sample_size",
        "E_INVALID_TREES",
    )
    sample_size = min(sample_size, n_rows)
    max_depth = params.get("max_depth", 0)
    if isinstance(max_depth, bool) or not isinstance(max_depth, int) or max_depth < 0:
        raise _business_error(
            "E_INVALID_TREES",
            f"{field}.max_depth",
            "max_depth must be 0 (auto) or a positive integer",
        )
    result: dict[str, Any] = {
        "n_trees": n_trees,
        "sample_size": sample_size,
        "max_depth": max_depth,
        "max_depth_auto": max_depth == 0,
    }
    if "seed" in params:
        result["seed"] = _positive_int(params["seed"], f"{field}.seed")
    return result


def _normalize_knn(params: dict[str, Any], n_rows: int, field: str) -> dict[str, Any]:
    allowed = {"n_neighbors", "metric", "seed"}
    unknown = sorted(set(params) - allowed)
    if unknown:
        raise _business_error(
            "E_UNKNOWN_HYPERPARAMETER",
            f"{field}.{unknown[0]}",
            f"unknown knn hyperparameter {unknown[0]!r}",
        )
    neighbors = _positive_int(
        params.get("n_neighbors", 5),
        f"{field}.n_neighbors",
        "E_INVALID_NEIGHBORS",
    )
    if neighbors >= n_rows:
        raise _business_error(
            "E_INVALID_NEIGHBORS",
            f"{field}.n_neighbors",
            f"n_neighbors ({neighbors}) must be smaller than sample count ({n_rows})",
        )
    metric = params.get("metric", "euclidean")
    if metric != "euclidean":
        raise _business_error(
            "E_INVALID_HYPERPARAMETER",
            f"{field}.metric",
            "only the exact euclidean metric is supported",
        )
    return {"n_neighbors": neighbors, "metric": "euclidean"}


def _normalize_mahalanobis(params: dict[str, Any], field: str) -> dict[str, Any]:
    allowed = {"regularization", "covariance", "seed"}
    unknown = sorted(set(params) - allowed)
    if unknown:
        raise _business_error(
            "E_UNKNOWN_HYPERPARAMETER",
            f"{field}.{unknown[0]}",
            f"unknown mahalanobis hyperparameter {unknown[0]!r}",
        )
    regularization = _positive_float(params.get("regularization", 1e-6), f"{field}.regularization")
    covariance = params.get("covariance", "full")
    if covariance != "full":
        raise _business_error(
            "E_INVALID_HYPERPARAMETER",
            f"{field}.covariance",
            "mahalanobis requires full covariance",
        )
    return {"regularization": regularization, "covariance": "full"}


def normalize_config(raw: Any, *, n_rows: int) -> EngineConfig:
    if not isinstance(raw, dict):
        raise _business_error("E_INVALID_CONFIG", "$", "configuration root must be an object")
    run_seed = raw.get("run_seed", raw.get("seed", 20261121))
    if isinstance(run_seed, bool) or not isinstance(run_seed, int) or run_seed < 0:
        raise _business_error("E_INVALID_CONFIG", "run_seed", "run_seed must be a non-negative integer")

    parallel = raw.get("parallel", {})
    if not isinstance(parallel, dict):
        raise _business_error("E_INVALID_CONFIG", "parallel", "parallel must be an object")
    workers = _positive_int(
        parallel.get("workers", 2),
        "parallel.workers",
        "E_INVALID_PARALLEL",
    )
    batch_size = _positive_int(
        parallel.get("batch_size", 64),
        "parallel.batch_size",
        "E_INVALID_BATCH_SIZE",
    )
    start_method = parallel.get("start_method", "spawn")
    if start_method not in {"spawn", "fork", "forkserver"}:
        raise _business_error(
            "E_INVALID_PARALLEL",
            "parallel.start_method",
            "start_method must be spawn, fork or forkserver",
        )

    budgets = raw.get("budgets", raw.get("budget", {}))
    if not isinstance(budgets, dict):
        raise _business_error("E_INVALID_CONFIG", "budgets", "budgets must be an object")
    max_comparisons = budgets.get(
        "max_comparisons",
        budgets.get("comparison_budget", budgets.get("total_comparison_budget")),
    )
    max_memory = budgets.get(
        "max_memory_bytes",
        budgets.get("memory_limit_bytes", budgets.get("memory_limit")),
    )
    if max_comparisons is None:
        raise _business_error("E_INVALID_BUDGET", "budgets.max_comparisons", "comparison budget is required")
    if max_memory is None:
        raise _business_error("E_INVALID_BUDGET", "budgets.max_memory_bytes", "memory budget is required")
    max_comparisons = _positive_int(
        max_comparisons,
        "budgets.max_comparisons",
        "E_INVALID_BUDGET",
    )
    max_memory = _positive_int(
        max_memory,
        "budgets.max_memory_bytes",
        "E_INVALID_BUDGET",
    )

    priority = raw.get("priority", {})
    if not isinstance(priority, dict):
        raise _business_error("E_INVALID_CONFIG", "priority", "priority must be an object")
    order = priority.get("order", ["quality", "memory", "progress"])
    if (
        not isinstance(order, list)
        or any(not isinstance(item, str) for item in order)
        or len(set(order)) != len(order)
        or set(order) != {"quality", "memory", "progress"}
    ):
        raise _business_error(
            "E_INVALID_CONFIG",
            "priority.order",
            "priority order must be a unique permutation of quality, memory, progress",
        )

    evaluation = raw.get("evaluation", {})
    if not isinstance(evaluation, dict):
        raise _business_error("E_INVALID_CONFIG", "evaluation", "evaluation must be an object")
    quota = evaluation.get("quota_fraction", raw.get("quota_fraction", 0.1))
    quota = _positive_float(quota, "evaluation.quota_fraction")
    if quota > 1.0:
        raise _business_error(
            "E_INVALID_CONFIG",
            "evaluation.quota_fraction",
            "quota_fraction must be in (0, 1]",
        )

    raw_candidates = _method_candidates(raw.get("methods"))
    if not raw_candidates:
        raise _business_error("E_INVALID_CONFIG", "methods", "at least one candidate is required")

    candidates: list[CandidateSpec] = []
    for method, index, params in raw_candidates:
        field = f"methods.{method}[{index}]"
        explicit_seed = params.get("seed")
        if method == "isolation_forest":
            normalized = _normalize_iforest(params, n_rows, field)
        elif method == "knn":
            normalized = _normalize_knn(params, n_rows, field)
        else:
            normalized = _normalize_mahalanobis(params, field)

        if explicit_seed is not None:
            if (
                isinstance(explicit_seed, bool)
                or not isinstance(explicit_seed, int)
                or explicit_seed < 0
            ):
                raise _business_error(
                    "E_INVALID_CONFIG",
                    f"{field}.seed",
                    "seed must be a non-negative integer",
                )
            method_seed = explicit_seed
        else:
            method_seed = derive_method_seed(run_seed, method)
        normalized["seed"] = method_seed
        candidate_id = f"{method}-{index:02d}"
        candidates.append(
            CandidateSpec(
                candidate_id=candidate_id,
                method=method,
                candidate_index=index,
                params=normalized,
                seed=method_seed,
            )
        )

    candidates.sort(
        key=lambda item: (
            METHOD_ORDER[item.method],
            item.candidate_index,
            item.candidate_id,
        )
    )
    return EngineConfig(
        run_seed=run_seed,
        candidates=tuple(candidates),
        workers=workers,
        batch_size=batch_size,
        start_method=start_method,
        max_comparisons=max_comparisons,
        max_memory_bytes=max_memory,
        priority_order=tuple(order),
        quota_fraction=quota,
    )
