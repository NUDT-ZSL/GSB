"""End-to-end benchmark, injection isolation and report generation."""

from __future__ import annotations

import hashlib
import platform
import sys
from pathlib import Path
from typing import Any, Mapping

import numpy as np

from .config import (
    DEFAULT_INJECTIONS,
    DEFAULT_REPORT,
    DEFAULT_SHAPE_SPECS,
    fingerprint,
    load_json,
)
from .engine import AdaptiveEngine
from .errors import GemmError
from .generator import generate_case
from .kernels import bmm, gemm
from .precision import (
    arrays_equal,
    error_summary,
    max_abs_difference,
    pair_key,
)
from .reporting import atomic_write_json


REQUIRED_CATEGORIES = ("small", "medium", "large_k", "large_batch")


def _validate_shape_config(config: Mapping[str, Any]) -> None:
    categories = config.get("categories")
    if not isinstance(categories, list):
        raise GemmError("E_CONFIG_INVALID", "shape config categories must be a list")
    names: set[str] = set()
    counts: dict[str, int] = {}
    for category in categories:
        if not isinstance(category, Mapping):
            raise GemmError("E_CONFIG_INVALID", "shape category must be an object")
        name = str(category.get("name", ""))
        specs = category.get("specs")
        if not name or name in names:
            raise GemmError(
                "E_CONFIG_INVALID",
                "shape category names must be non-empty and unique",
                {"name": name},
            )
        names.add(name)
        counts[name] = len(specs) if isinstance(specs, list) else 0
    missing = [name for name in REQUIRED_CATEGORIES if counts.get(name, 0) < 3]
    if missing:
        raise GemmError(
            "E_CONFIG_INVALID",
            "small, medium, large_k and large_batch need at least three shapes each",
            {"missing_or_short_categories": missing, "counts": counts},
        )


def _manifest(shape_config: Mapping[str, Any]) -> list[dict[str, Any]]:
    defaults = shape_config.get("defaults", {})
    manifest: list[dict[str, Any]] = []
    for category in shape_config["categories"]:
        for spec in category["specs"]:
            manifest.append(
                {
                    "shape_id": spec["shape_id"],
                    "category": category["name"],
                    "M": int(spec["M"]),
                    "N": int(spec["N"]),
                    "K": int(spec["K"]),
                    "batch": int(spec["batch"]),
                    "alpha": float(spec.get("alpha", defaults.get("alpha", 1.0))),
                    "beta": float(spec.get("beta", defaults.get("beta", 0.0))),
                    "cache_budget_bytes": int(
                        spec.get(
                            "cache_budget_bytes", defaults.get("cache_budget_bytes", 262144)
                        )
                    ),
                }
            )
    return manifest


def _benchmark_candidate(
    engine: AdaptiveEngine,
    case: Any,
    alpha: float,
    beta: float,
    candidate: Mapping[str, Any],
    max_workers: int,
) -> tuple[dict[str, Any], np.ndarray, Any]:
    execution = engine.execute_candidate_bmm(
        case.A,
        case.B,
        case.C,
        alpha=alpha,
        beta=beta,
        candidate=candidate,
        max_workers=max_workers,
    )
    record = {
        "strategy_id": candidate["strategy_id"],
        "tile": dict(candidate["tile"]),
        "loop_order": candidate["loop_order"],
        "batch_schedule": candidate["batch_schedule"],
        "eligible": candidate["eligible"],
        "eligibility_reason": candidate["eligibility_reason"],
        "estimated_cost_seconds": float(candidate["estimated_cost_seconds"]),
        "actual_seconds": float(execution.elapsed_seconds),
        "actual_to_estimated_ratio": (
            float(execution.elapsed_seconds / candidate["estimated_cost_seconds"])
            if candidate["estimated_cost_seconds"] > 0.0
            else None
        ),
        "execution_statistics": execution.statistics,
        "execution_scheduler": execution.scheduler,
    }
    return record, execution.output, execution


def _run_concurrency_validation(
    engine: AdaptiveEngine,
    case: Any,
    alpha: float,
    beta: float,
    decision: Mapping[str, Any],
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    base_candidate = {
        "strategy_id": decision["strategy_id"],
        "tile": decision["tile"],
        "loop_order": decision["loop_order"],
    }
    serial = engine.execute_candidate_bmm(
        case.A,
        case.B,
        case.C,
        alpha=alpha,
        beta=beta,
        candidate={**base_candidate, "batch_schedule": "serial"},
        max_workers=1,
    )
    parallel_workers = max(2, engine.max_workers)
    parallel_single = engine.execute_candidate_bmm(
        case.A,
        case.B,
        case.C,
        alpha=alpha,
        beta=beta,
        candidate={**base_candidate, "batch_schedule": "batch_parallel"},
        max_workers=1,
    )
    parallel_multiple = engine.execute_candidate_bmm(
        case.A,
        case.B,
        case.C,
        alpha=alpha,
        beta=beta,
        candidate={**base_candidate, "batch_schedule": "batch_parallel"},
        max_workers=parallel_workers,
    )
    flattened = engine.execute_candidate_bmm(
        case.A,
        case.B,
        case.C,
        alpha=alpha,
        beta=beta,
        candidate={**base_candidate, "batch_schedule": "flattened"},
        max_workers=parallel_workers,
    )
    executions = {
        "serial": serial,
        "parallel_1": parallel_single,
        "parallel_multi": parallel_multiple,
        "flattened": flattened,
    }
    outputs_equal = all(
        arrays_equal(serial.output, execution.output)
        for execution in executions.values()
    )
    statistics_equal = all(
        serial.statistics == execution.statistics
        for execution in executions.values()
    )
    batch_decisions_equal = all(
        serial.batch_decisions == execution.batch_decisions
        for execution in executions.values()
    )
    validation = {
        "strategy_id": decision["strategy_id"],
        "tile": dict(decision["tile"]),
        "loop_order": decision["loop_order"],
        "schedules_compared": ["serial", "batch_parallel", "flattened"],
        "parallel_workers_compared": [1, parallel_workers],
        "all_schedule_outputs_bitwise_equal": outputs_equal,
        "max_abs_difference": max_abs_difference(serial.output, flattened.output),
        "deterministic_statistics_equal": statistics_equal,
        "batch_decision_order_equal": batch_decisions_equal,
        "canonical_statistics": serial.statistics,
        "scheduler_metadata": {
            name: execution.scheduler for name, execution in executions.items()
        },
        "decision_independent_of_worker_count": True,
    }
    failures: list[dict[str, Any]] = []
    if not (outputs_equal and statistics_equal and batch_decisions_equal):
        failures.append(
            {
                "source": "concurrency_validation",
                "shape_id": case.shape_id,
                "code": "E_CONCURRENCY_INCONSISTENCY",
                "message": "worker count changed output or deterministic statistics",
                "validation": validation,
            }
        )
    return validation, failures


def _run_shape(
    engine: AdaptiveEngine,
    case: Any,
    alpha: float,
    beta: float,
    cache_budget_bytes: int,
    max_workers: int,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    decision = engine.select_bmm(
        case.M,
        case.N,
        case.K,
        batch=case.batch,
        alpha=alpha,
        beta=beta,
        cache_budget_bytes=cache_budget_bytes,
    )
    failures: list[dict[str, Any]] = []
    candidate_records: list[dict[str, Any]] = []
    candidate_outputs: dict[str, np.ndarray] = {}
    candidate_executions: dict[str, Any] = {}
    benchmark_limit = (
        len(decision["candidates"])
        if engine.runtime.benchmark_all_candidates
        else 1
    )
    candidates_to_run = decision["candidates"][:benchmark_limit]
    if not engine.runtime.benchmark_all_candidates:
        candidates_to_run = [
            candidate
            for candidate in decision["candidates"]
            if candidate["strategy_id"] == decision["strategy_id"]
        ]
    for candidate in candidates_to_run:
        record, output, execution = _benchmark_candidate(
            engine, case, alpha, beta, candidate, max_workers
        )
        record["selected"] = candidate["strategy_id"] == decision["strategy_id"]
        candidate_records.append(record)
        candidate_outputs[candidate["strategy_id"]] = output
        candidate_executions[candidate["strategy_id"]] = execution
    if decision["strategy_id"] not in candidate_outputs:
        selected_candidate = next(
            candidate
            for candidate in decision["candidates"]
            if candidate["strategy_id"] == decision["strategy_id"]
        )
        record, output, execution = _benchmark_candidate(
            engine, case, alpha, beta, selected_candidate, max_workers
        )
        record["selected"] = True
        candidate_records.append(record)
        candidate_outputs[decision["strategy_id"]] = output
        candidate_executions[decision["strategy_id"]] = execution
    selected_output = candidate_outputs[decision["strategy_id"]]

    for record in candidate_records:
        output = candidate_outputs[record["strategy_id"]]
        record["max_abs_difference_vs_selected"] = max_abs_difference(
            output, selected_output
        )
        record["consistent_with_selected_within_tolerance"] = bool(
            np.allclose(
                output,
                selected_output,
                rtol=engine.runtime.relative_tolerance,
                atol=engine.runtime.absolute_tolerance,
                equal_nan=False,
            )
        )
        record["output_finite"] = bool(np.isfinite(output).all())
        if not record["consistent_with_selected_within_tolerance"]:
            failures.append(
                {
                    "source": "strategy_consistency",
                    "shape_id": case.shape_id,
                    "code": "E_NUMERIC_INCONSISTENCY",
                    "message": "strategy output exceeds configured tolerance",
                    "strategy_id": record["strategy_id"],
                    "max_abs_difference": record["max_abs_difference_vs_selected"],
                }
            )

    reference = np.empty_like(selected_output, dtype=np.float64)
    A64 = np.asarray(case.A, dtype=np.float64)
    B64 = np.asarray(case.B, dtype=np.float64)
    C64 = np.asarray(case.C, dtype=np.float64) if beta != 0.0 else None
    for batch_index in range(case.batch):
        reference[batch_index] = np.matmul(A64[batch_index], B64[batch_index]) * alpha
        if C64 is not None:
            reference[batch_index] += beta * C64[batch_index]
    accuracy = error_summary(
        selected_output,
        reference,
        relative_epsilon=engine.runtime.relative_error_epsilon,
    )

    unblocked = bmm(
        case.A,
        case.B,
        case.C,
        alpha,
        beta,
        tile=None,
        loop_order="ijk",
        batch_schedule="serial",
        max_workers=1,
    )
    loop_outputs: dict[str, np.ndarray] = {}
    for record in candidate_records:
        loop_outputs.setdefault(record["loop_order"], candidate_outputs[record["strategy_id"]])
    loop_pairwise: dict[str, float] = {}
    loop_names = sorted(loop_outputs)
    for left_index, left_name in enumerate(loop_names):
        for right_name in loop_names[left_index + 1 :]:
            loop_pairwise[pair_key(left_name, right_name)] = max_abs_difference(
                loop_outputs[left_name], loop_outputs[right_name]
            )
    sensitivity = {
        "unblocked_float32_max_abs_difference": max_abs_difference(
            selected_output, unblocked
        ),
        "unblocked_float32_allclose": bool(
            np.allclose(
                selected_output,
                unblocked,
                rtol=engine.runtime.relative_tolerance,
                atol=engine.runtime.absolute_tolerance,
                equal_nan=False,
            )
        ),
        "loop_order_pairwise_max_abs_difference": loop_pairwise,
        "interpretation": "Pairwise differences quantify sensitivity to block/accumulation order; tolerance is configured in runtime.json.",
    }

    concurrency, concurrency_failures = _run_concurrency_validation(
        engine, case, alpha, beta, decision
    )
    failures.extend(concurrency_failures)
    recheck = engine.select_bmm(
        case.M,
        case.N,
        case.K,
        batch=case.batch,
        alpha=alpha,
        beta=beta,
        cache_budget_bytes=cache_budget_bytes,
    )
    cache_recheck = {
        "cache_hit": bool(recheck["cache_hit"]),
        "cache_key_same": recheck["cache_key"] == decision["cache_key"],
        "same_strategy": recheck["strategy_id"] == decision["strategy_id"],
        "same_tile": recheck["tile"] == decision["tile"],
        "same_loop_order": recheck["loop_order"] == decision["loop_order"],
        "same_batch_schedule": recheck["batch_schedule"] == decision["batch_schedule"],
    }
    if not all(cache_recheck.values()):
        failures.append(
            {
                "source": "cache_validation",
                "shape_id": case.shape_id,
                "code": "E_CACHE_INCONSISTENCY",
                "message": "repeated selection did not return the cached decision",
                "cache_recheck": cache_recheck,
            }
        )

    selected_record = next(
        record for record in candidate_records if record["strategy_id"] == decision["strategy_id"]
    )
    selected_execution = candidate_executions[decision["strategy_id"]]
    shape_record = {
        "shape_id": case.shape_id,
        "category": case.category,
        "shape": {
            "M": case.M,
            "N": case.N,
            "K": case.K,
            "batch": case.batch,
            "alpha": alpha,
            "beta": beta,
            "cache_budget_bytes": cache_budget_bytes,
        },
        "input_data": case.metadata(),
        "selected_strategy": {
            "strategy_id": decision["strategy_id"],
            "tile": decision["tile"],
            "loop_order": decision["loop_order"],
            "batch_schedule": decision["batch_schedule"],
            "reason_code": decision["reason_code"],
            "estimated_cost_seconds": decision["selected_estimated_cost_seconds"],
            "actual_seconds": selected_record["actual_seconds"],
        },
        "decision": decision,
        "cache_recheck": cache_recheck,
        "accuracy": accuracy,
        "accumulation_order_sensitivity": sensitivity,
        "strategy_benchmarks": candidate_records,
        "execution_statistics": selected_execution.statistics,
        "execution_scheduler": selected_execution.scheduler,
        "batch_decision_order": selected_execution.batch_decisions,
        "concurrency_consistency": concurrency,
    }
    return shape_record, failures


def _base_injection_matrices(
    M: int,
    N: int,
    K: int,
    batch: int,
    *,
    seed: int,
) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    rng = np.random.default_rng(seed)
    A = (rng.standard_normal((batch, M, K), dtype=np.float32) * np.float32(0.05)).astype(
        np.float32, copy=False
    )
    B = (rng.standard_normal((batch, K, N), dtype=np.float32) * np.float32(0.05)).astype(
        np.float32, copy=False
    )
    C = (rng.standard_normal((batch, M, N), dtype=np.float32) * np.float32(0.05)).astype(
        np.float32, copy=False
    )
    return A, B, C


def _inject_nonfinite(
    item: Mapping[str, Any],
    engine: AdaptiveEngine,
) -> None:
    M = int(item["M"])
    N = int(item["N"])
    K = int(item["K"])
    batch = int(item["batch"])
    injection_seed = int.from_bytes(
        hashlib.sha256(str(item["injection_id"]).encode("utf-8")).digest()[:8],
        "little",
    )
    A, B, C = _base_injection_matrices(
        M, N, K, batch, seed=int(item.get("seed", 9001)) + injection_seed
    )
    matrices = {"A": A, "B": B, "C": C}
    matrix = str(item["matrix"])
    value_text = str(item.get("value", "nan")).lower()
    value = {
        "nan": np.float32(np.nan),
        "inf": np.float32(np.inf),
        "-inf": np.float32(-np.inf),
    }[value_text]
    target = matrices[matrix].reshape(batch, -1)
    count = int(item.get("nonfinite_count", 1))
    target[:, :count] = value
    alpha = float(item.get("alpha", 1.0))
    beta = float(item.get("beta", 0.0))
    if batch > 1:
        bmm(A, B, C, alpha, beta, tile=None, batch_schedule="serial", max_workers=1)
    else:
        gemm(A[0], B[0], C[0], alpha, beta)


def _run_injection_item(item: Mapping[str, Any], engine: AdaptiveEngine) -> dict[str, Any]:
    expected_code = str(item["expected_code"])
    injection_id = str(item["injection_id"])
    kind = str(item["kind"])
    observed_code = "NO_ERROR"
    error_message = ""
    error_details: Mapping[str, Any] = {}
    try:
        if kind == "nonfinite":
            _inject_nonfinite(item, engine)
        elif kind == "shape_mismatch":
            M, N, K = int(item["M"]), int(item["N"]), int(item["K"])
            batch = int(item["batch"])
            A, B, C = _base_injection_matrices(
                M, N, K, batch, seed=int(item.get("seed", 7001))
            )
            mismatch = str(item["mismatch"])
            if mismatch == "gemm_inner":
                gemm(A[0], B[0, : max(1, K - 1), :], C[0])
            elif mismatch == "bmm_batch":
                bmm(A, B[: max(1, batch - 1)], C)
            else:
                raise GemmError(
                    "E_INJECTION_INVALID",
                    "unknown shape mismatch injection",
                    {"mismatch": mismatch},
                )
        elif kind == "tile_invalid":
            M, N, K = int(item["M"]), int(item["N"]), int(item["K"])
            batch = int(item["batch"])
            A, B, C = _base_injection_matrices(
                M, N, K, batch, seed=int(item.get("seed", 6001))
            )
            gemm(
                A[0],
                B[0],
                C[0],
                tile=item["tile"],
                max_tile_elements=engine.runtime.max_tile_elements,
                max_tile_dimension=engine.runtime.max_tile_dimension,
            )
        elif kind == "budget_zero":
            engine.select_bmm(
                int(item["M"]),
                int(item["N"]),
                int(item["K"]),
                batch=int(item["batch"]),
                alpha=item.get("alpha", 1.0),
                beta=item.get("beta", 0.0),
                cache_budget_bytes=int(item["cache_budget_bytes"]),
            )
        else:
            raise GemmError(
                "E_INJECTION_INVALID",
                "unknown injection kind",
                {"kind": kind},
            )
    except GemmError as exc:
        observed_code = exc.code
        error_message = exc.message
        error_details = exc.details
    except Exception as exc:
        observed_code = "E_INTERNAL"
        error_message = f"{type(exc).__name__}: {exc}"
        error_details = {"exception_type": type(exc).__name__}
    matched = observed_code == expected_code
    return {
        "injection_id": injection_id,
        "kind": kind,
        "expected_code": expected_code,
        "observed_code": observed_code,
        "matched": matched,
        "error_message": error_message,
        "error_details": dict(error_details),
    }


def _run_injections(
    config: Mapping[str, Any], engine: AdaptiveEngine
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    items = config.get("items")
    if not isinstance(items, list):
        raise GemmError("E_CONFIG_INVALID", "injection config items must be a list")
    results: list[dict[str, Any]] = []
    failures: list[dict[str, Any]] = []
    for item in items:
        if not isinstance(item, Mapping):
            result = {
                "injection_id": "UNKNOWN",
                "kind": "invalid",
                "expected_code": "E_INJECTION_INVALID",
                "observed_code": "E_INJECTION_INVALID",
                "matched": False,
                "error_message": "injection item must be an object",
                "error_details": {},
            }
        else:
            result = _run_injection_item(item, engine)
        results.append(result)
        if not result["matched"]:
            failures.append(
                {
                    "source": "injection",
                    "injection_id": result["injection_id"],
                    "code": result["expected_code"],
                    "observed_code": result["observed_code"],
                    "message": result["error_message"],
                }
            )
    hit_ids = [result["injection_id"] for result in results if result["matched"]]
    summary = {
        "total": len(results),
        "matched_count": len(hit_ids),
        "failure_count": len(failures),
        "hit_injection_ids": hit_ids,
        "results": results,
        "failure_list": failures,
        "isolation_preserved": len(results) == len(items),
    }
    return summary, failures


def run_suite(
    *,
    shape_specs_path: str | Path = DEFAULT_SHAPE_SPECS,
    injection_config_path: str | Path = DEFAULT_INJECTIONS,
    output_path: str | Path = DEFAULT_REPORT,
    max_workers: int | None = None,
    strategy_table_path: str | Path | None = None,
    cost_model_path: str | Path | None = None,
    runtime_path: str | Path | None = None,
) -> dict[str, Any]:
    shape_config = load_json(shape_specs_path)
    injection_config = load_json(injection_config_path)
    _validate_shape_config(shape_config)
    engine_kwargs: dict[str, Any] = {}
    if strategy_table_path is not None:
        engine_kwargs["strategy_table_path"] = strategy_table_path
    if cost_model_path is not None:
        engine_kwargs["cost_model_path"] = cost_model_path
    if runtime_path is not None:
        engine_kwargs["runtime_path"] = runtime_path
    if max_workers is not None:
        engine_kwargs["max_workers"] = max_workers
    engine = AdaptiveEngine(**engine_kwargs)
    manifest = _manifest(shape_config)
    defaults = shape_config.get("defaults", {})
    shape_records: list[dict[str, Any]] = []
    failures: list[dict[str, Any]] = []
    for category in shape_config["categories"]:
        category_name = str(category["name"])
        for spec in category["specs"]:
            case = generate_case(spec, int(shape_config.get("seed", 0)), category=category_name)
            alpha = float(spec.get("alpha", defaults.get("alpha", 1.0)))
            beta = float(spec.get("beta", defaults.get("beta", 0.0)))
            budget = int(
                spec.get(
                    "cache_budget_bytes", defaults.get("cache_budget_bytes", 262144)
                )
            )
            shape_record, shape_failures = _run_shape(
                engine,
                case,
                alpha,
                beta,
                budget,
                engine.max_workers,
            )
            shape_records.append(shape_record)
            failures.extend(shape_failures)
    injection_summary, injection_failures = _run_injections(injection_config, engine)
    failures.extend(injection_failures)
    cache_summary = engine.cache_stats()
    selected_costs = [
        record["selected_strategy"]["estimated_cost_seconds"] for record in shape_records
    ]
    selected_actuals = [
        record["selected_strategy"]["actual_seconds"] for record in shape_records
    ]
    max_abs_errors = [record["accuracy"]["max_absolute_error"] for record in shape_records]
    report = {
        "schema_version": "1.0",
        "component": "adaptive_gemm_bmm",
        "environment": {
            "python": sys.version.split()[0],
            "numpy": np.__version__,
            "platform": platform.platform(),
            "machine": platform.machine(),
            "cpu_count": __import__("os").cpu_count(),
        },
        "configuration": {
            "shape_specs_path": str(Path(shape_specs_path)),
            "injection_config_path": str(Path(injection_config_path)),
            "strategy_table_path": str(engine.strategy_table_path),
            "cost_model_path": str(engine.cost_model_path),
            "runtime_path": str(engine.runtime_path),
            "shape_seed": int(shape_config.get("seed", 0)),
            "strategy_table_fingerprint": engine.selector.table_fingerprint,
            "strategy_table": engine.table_raw,
            "cost_model": engine.cost_raw,
            "runtime": engine.runtime.as_dict(),
            "effective_max_workers": engine.max_workers,
            "shape_config_sha256": fingerprint(shape_config),
            "injection_config_sha256": fingerprint(injection_config),
        },
        "shape_manifest": manifest,
        "decision_order": [record["shape_id"] for record in shape_records],
        "shapes": shape_records,
        "cache": {
            **cache_summary,
            "key_components": [
                "shape.M",
                "shape.N",
                "shape.K",
                "batch",
                "alpha",
                "beta",
                "cache_budget_bytes",
                "strategy_table_fingerprint",
            ],
            "invalidation_behavior": "explicit invalidate(key) or invalidate() increments cache_invalidations",
        },
        "strategies": [strategy.as_dict() for strategy in engine.strategies],
        "performance_summary": {
            "selected_estimated_seconds_total": float(sum(selected_costs)),
            "selected_actual_seconds_total": float(sum(selected_actuals)),
            "selected_actual_to_estimated_ratio": (
                float(sum(selected_actuals) / sum(selected_costs))
                if sum(selected_costs) > 0.0
                else None
            ),
            "max_absolute_error_across_shapes": float(max(max_abs_errors)),
        },
        "injection": injection_summary,
        "failure_list": failures,
        "validation_summary": {
            "shape_count": len(shape_records),
            "category_counts": {
                name: sum(1 for record in shape_records if record["category"] == name)
                for name in REQUIRED_CATEGORIES
            },
            "all_strategies_consistent": not any(
                failure["source"] == "strategy_consistency" for failure in failures
            ),
            "concurrency_consistent": not any(
                failure["source"] == "concurrency_validation" for failure in failures
            ),
            "cache_consistent": not any(
                failure["source"] == "cache_validation" for failure in failures
            ),
            "all_injections_matched": injection_summary["failure_count"] == 0,
            "passed": len(failures) == 0,
        },
    }
    atomic_write_json(output_path, report)
    return report
