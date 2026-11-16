"""Shape-aware strategy selection with an auditable cache."""

from __future__ import annotations

import copy
import hashlib
import math
from typing import Any, Mapping

import numpy as np

from .config import CostModel, Strategy, Tile, canonical_json, fingerprint, parse_tile
from .errors import GemmError
from .kernels import validate_tile


def _ceil_div(value: int, divisor: int) -> int:
    return (value + divisor - 1) // divisor


def _shape_int(name: str, value: Any) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise GemmError(
            "E_SHAPE_MISMATCH",
            f"{name} must be a positive integer",
            {"field": name, "value": value},
        )
    return value


def _budget_int(value: Any) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise GemmError(
            "E_BUDGET_INVALID",
            "cache_budget_bytes must be a positive integer",
            {"cache_budget_bytes": value},
        )
    return value


def _scalar_token(name: str, value: Any) -> str:
    array = np.asarray(value)
    if array.ndim != 0:
        raise GemmError(
            "E_SHAPE_MISMATCH",
            f"{name} must be a scalar",
            {"parameter": name, "shape": list(array.shape)},
        )
    try:
        numeric = float(value)
    except (TypeError, ValueError, OverflowError) as exc:
        raise GemmError(
            "E_NONFINITE_INPUT",
            f"{name} must be a finite scalar",
            {"parameter": name, "value": str(value)},
        ) from exc
    if not math.isfinite(numeric):
        raise GemmError(
            "E_NONFINITE_INPUT",
            f"{name} must be finite",
            {"parameter": name, "value": numeric},
        )
    return numeric.hex()


class StrategySelector:
    def __init__(
        self,
        strategies: list[Strategy],
        cost_model: CostModel,
        *,
        strategy_table_raw: Mapping[str, Any] | None = None,
        max_tile_elements: int = 1_048_576,
        max_tile_dimension: int = 4096,
    ) -> None:
        if not strategies:
            raise GemmError("E_CONFIG_INVALID", "at least one strategy is required")
        self.strategies = tuple(strategies)
        self.cost_model = cost_model
        self.max_tile_elements = max_tile_elements
        self.max_tile_dimension = max_tile_dimension
        for strategy in self.strategies:
            validate_tile(
                strategy.tile,
                max_tile_elements=max_tile_elements,
                max_tile_dimension=max_tile_dimension,
            )
        table_payload = dict(strategy_table_raw or {})
        self.table_fingerprint = fingerprint(
            {
                "strategy_table": table_payload,
                "cost_model": cost_model.as_dict(),
                "max_tile_elements": max_tile_elements,
                "max_tile_dimension": max_tile_dimension,
            }
        )
        self._cache: dict[str, dict[str, Any]] = {}
        self._cache_hits = 0
        self._cache_misses = 0
        self._cache_invalidations = 0

    def _cache_key(
        self,
        M: int,
        N: int,
        K: int,
        batch: int,
        alpha: Any,
        beta: Any,
        cache_budget_bytes: int,
    ) -> tuple[str, dict[str, Any]]:
        components = {
            "shape": {"M": M, "N": N, "K": K},
            "batch": batch,
            "alpha": _scalar_token("alpha", alpha),
            "beta": _scalar_token("beta", beta),
            "cache_budget_bytes": cache_budget_bytes,
            "strategy_table_fingerprint": self.table_fingerprint,
        }
        key = hashlib.sha256(canonical_json(components).encode("utf-8")).hexdigest()
        return key, components

    def _candidate_record(
        self,
        strategy: Strategy,
        M: int,
        N: int,
        K: int,
        batch: int,
        cache_budget_bytes: int,
    ) -> dict[str, Any]:
        model = self.cost_model
        tile = strategy.tile
        tile_count = (
            _ceil_div(M, tile.BM) * _ceil_div(N, tile.BN) * _ceil_div(K, tile.BK)
        )
        padded_M = _ceil_div(M, tile.BM) * tile.BM
        padded_N = _ceil_div(N, tile.BN) * tile.BN
        padded_K = _ceil_div(K, tile.BK) * tile.BK
        flops = 2 * M * N * K * batch
        padded_flops = 2 * padded_M * padded_N * padded_K * batch
        memory_bytes = 4 * (batch * M * K + batch * K * N + batch * M * N)
        if strategy.loop_order == "ijk":
            accumulator_elements = tile.BM * tile.BN
        elif strategy.loop_order == "ikj":
            accumulator_elements = tile.BM * N
        else:
            accumulator_elements = M * tile.BN
        working_set_bytes = 4 * (
            tile.BM * tile.BK + tile.BK * tile.BN + accumulator_elements
        )
        compute_seconds = flops / model.flops_per_second
        memory_seconds = memory_bytes / model.memory_bytes_per_second
        wasted_compute_seconds = (padded_flops - flops) / model.flops_per_second
        tile_overhead_seconds = tile_count * batch * model.tile_overhead_seconds
        cache_pressure = max(0.0, working_set_bytes / cache_budget_bytes - 1.0)
        cache_penalty_seconds = (
            cache_pressure**model.cache_pressure_exponent
            * model.cache_miss_penalty_scale
            * (flops / 1_000_000.0)
        )
        if strategy.batch_schedule == "serial":
            schedule_seconds = batch * model.task_overhead_seconds
            schedule_eligible = True
            schedule_reason = "ELIGIBLE_SERIAL"
        elif strategy.batch_schedule == "batch_parallel":
            schedule_seconds = (
                _ceil_div(batch, model.nominal_parallel_workers)
                * model.task_overhead_seconds
                + batch * model.parallel_sync_seconds
            )
            schedule_eligible = batch >= model.batch_parallel_min_batch
            schedule_reason = (
                "ELIGIBLE_BATCH_PARALLEL"
                if schedule_eligible
                else "INELIGIBLE_BATCH_BELOW_PARALLEL_MIN"
            )
        else:
            schedule_seconds = (
                _ceil_div(batch, model.flatten_chunk_size) * model.task_overhead_seconds
            )
            schedule_eligible = batch >= model.flatten_min_batch
            schedule_reason = (
                "ELIGIBLE_FLATTENED"
                if schedule_eligible
                else "INELIGIBLE_BATCH_BELOW_FLATTENED_MIN"
            )
        estimated_seconds = (
            compute_seconds
            + memory_seconds
            + wasted_compute_seconds
            + tile_overhead_seconds
            + cache_penalty_seconds
            + schedule_seconds
        )
        padding_efficiency = flops / padded_flops if padded_flops else 0.0
        return {
            "strategy_id": strategy.strategy_id,
            "tile": tile.as_dict(),
            "loop_order": strategy.loop_order,
            "batch_schedule": strategy.batch_schedule,
            "tags": list(strategy.tags),
            "eligible": schedule_eligible,
            "eligibility_reason": schedule_reason,
            "estimated_cost_seconds": estimated_seconds,
            "cost_components": {
                "flops": flops,
                "padded_flops": padded_flops,
                "single_batch_elements": M * N * K,
                "total_elements": M * N * K * batch,
                "memory_bytes": memory_bytes,
                "tile_count": tile_count,
                "accumulator_elements": accumulator_elements,
                "working_set_bytes": working_set_bytes,
                "cache_pressure": cache_pressure,
                "compute_seconds": compute_seconds,
                "memory_seconds": memory_seconds,
                "wasted_compute_seconds": wasted_compute_seconds,
                "tile_overhead_seconds": tile_overhead_seconds,
                "cache_penalty_seconds": cache_penalty_seconds,
                "schedule_seconds": schedule_seconds,
            },
            "cache_fit": working_set_bytes <= cache_budget_bytes,
            "padding_efficiency": padding_efficiency,
        }

    def _reason(self, selected: Mapping[str, Any], batch: int, M: int, N: int, K: int) -> tuple[str, str]:
        schedule = selected["batch_schedule"]
        if batch == 1 and M * N * K <= 4096:
            return (
                "R_SMALL_SHAPE_LOW_OVERHEAD",
                "Smallest eligible shape has the lowest setup and boundary cost.",
            )
        if schedule == "flattened":
            return (
                "R_FLATTENED_BATCH_THROUGHPUT",
                "Flattened batch chunks have the lowest estimated scheduling overhead.",
            )
        if schedule == "batch_parallel":
            return (
                "R_BATCH_PARALLEL_MIN_COST",
                "Batch-parallel execution has the lowest estimated total cost.",
            )
        if selected["cache_fit"] and selected["padding_efficiency"] >= 0.8:
            return (
                "R_CACHE_FIT_LOW_PADDING",
                "Selected tile fits the cache budget with low boundary waste.",
            )
        return (
            "R_MIN_ESTIMATED_COST",
            "Selected candidate has the minimum estimated cost across eligible strategies.",
        )

    def select(
        self,
        M: int,
        N: int,
        K: int,
        *,
        batch: int = 1,
        alpha: Any = 1.0,
        beta: Any = 0.0,
        cache_budget_bytes: int,
    ) -> dict[str, Any]:
        M = _shape_int("M", M)
        N = _shape_int("N", N)
        K = _shape_int("K", K)
        batch = _shape_int("batch", batch)
        budget = _budget_int(cache_budget_bytes)
        key, components = self._cache_key(M, N, K, batch, alpha, beta, budget)
        if key in self._cache:
            self._cache_hits += 1
            decision = copy.deepcopy(self._cache[key])
            decision["cache_hit"] = True
            decision["cache_status"] = "HIT"
            return decision

        self._cache_misses += 1
        candidates = [
            self._candidate_record(strategy, M, N, K, batch, budget)
            for strategy in self.strategies
        ]
        eligible = [candidate for candidate in candidates if candidate["eligible"]]
        if not eligible:
            raise GemmError(
                "E_CONFIG_INVALID",
                "strategy table has no eligible candidate for this input",
                {"shape": [M, N, K], "batch": batch},
            )
        selected = min(
            eligible, key=lambda item: (item["estimated_cost_seconds"], item["strategy_id"])
        )
        reason_code, reason = self._reason(selected, batch, M, N, K)
        decision = {
            "cache_key": key,
            "cache_hit": False,
            "cache_status": "MISS",
            "inputs": {
                **components,
                "M": M,
                "N": N,
                "K": K,
                "batch": batch,
                "mkn_product": M * N * K,
                "single_batch_elements": M * N * K,
                "total_elements": M * N * K * batch,
                "cache_budget_bytes": budget,
            },
            "strategy_id": selected["strategy_id"],
            "tile": dict(selected["tile"]),
            "loop_order": selected["loop_order"],
            "batch_schedule": selected["batch_schedule"],
            "selected_estimated_cost_seconds": selected["estimated_cost_seconds"],
            "selected_cost_components": dict(selected["cost_components"]),
            "reason_code": reason_code,
            "reason": reason,
            "candidates": candidates,
            "cost_model": self.cost_model.as_dict(),
            "table_fingerprint": self.table_fingerprint,
        }
        self._cache[key] = copy.deepcopy(decision)
        return decision

    def invalidate(self, cache_key: str | None = None) -> int:
        if cache_key is None:
            count = len(self._cache)
            self._cache.clear()
        else:
            count = 1 if self._cache.pop(cache_key, None) is not None else 0
        self._cache_invalidations += count
        return count

    def cache_stats(self) -> dict[str, int]:
        return {
            "cache_hits": self._cache_hits,
            "cache_misses": self._cache_misses,
            "cache_invalidations": self._cache_invalidations,
            "cache_entries": len(self._cache),
        }


def strategy_from_decision(decision: Mapping[str, Any]) -> Strategy:
    tile = parse_tile(decision["tile"])
    return Strategy(
        strategy_id=str(decision["strategy_id"]),
        tile=Tile(tile.BM, tile.BN, tile.BK),
        loop_order=str(decision["loop_order"]),
        batch_schedule=str(decision["batch_schedule"]),
        tags=(),
    )
