"""Integrable high-level API for adaptive GEMM and BMM execution."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

import numpy as np

from .config import (
    DEFAULT_COST_MODEL,
    DEFAULT_RUNTIME,
    DEFAULT_STRATEGY_TABLE,
    CostModel,
    RuntimeConfig,
    Strategy,
    load_cost_model,
    load_runtime,
    load_strategy_table,
)
from .kernels import BmmExecution, execute_bmm, gemm
from .selector import StrategySelector


@dataclass
class AdaptiveResult:
    output: np.ndarray
    decision: dict[str, Any]
    execution: BmmExecution | None = None


class AdaptiveEngine:
    def __init__(
        self,
        *,
        strategy_table_path: str | Path = DEFAULT_STRATEGY_TABLE,
        cost_model_path: str | Path = DEFAULT_COST_MODEL,
        runtime_path: str | Path = DEFAULT_RUNTIME,
        max_workers: int | None = None,
    ) -> None:
        table_raw, strategies = load_strategy_table(strategy_table_path)
        cost_raw, cost_model = load_cost_model(cost_model_path)
        runtime_raw, runtime = load_runtime(runtime_path)
        self.strategy_table_path = Path(strategy_table_path)
        self.cost_model_path = Path(cost_model_path)
        self.runtime_path = Path(runtime_path)
        self.table_raw = table_raw
        self.cost_raw = cost_raw
        self.runtime_raw = runtime_raw
        self.strategies: list[Strategy] = strategies
        self.cost_model: CostModel = cost_model
        self.runtime: RuntimeConfig = runtime
        self.max_workers = runtime.max_workers if max_workers is None else max_workers
        if self.max_workers <= 0:
            from .errors import GemmError

            raise GemmError(
                "E_CONFIG_INVALID",
                "max_workers must be positive",
                {"max_workers": self.max_workers},
            )
        self.selector = StrategySelector(
            strategies,
            cost_model,
            strategy_table_raw=table_raw,
            max_tile_elements=runtime.max_tile_elements,
            max_tile_dimension=runtime.max_tile_dimension,
        )

    def select_gemm(
        self,
        M: int,
        N: int,
        K: int,
        *,
        alpha: Any,
        beta: Any,
        cache_budget_bytes: int,
    ) -> dict[str, Any]:
        return self.selector.select(
            M,
            N,
            K,
            batch=1,
            alpha=alpha,
            beta=beta,
            cache_budget_bytes=cache_budget_bytes,
        )

    def select_bmm(
        self,
        M: int,
        N: int,
        K: int,
        *,
        batch: int,
        alpha: Any,
        beta: Any,
        cache_budget_bytes: int,
    ) -> dict[str, Any]:
        return self.selector.select(
            M,
            N,
            K,
            batch=batch,
            alpha=alpha,
            beta=beta,
            cache_budget_bytes=cache_budget_bytes,
        )

    def adaptive_gemm(
        self,
        A: Any,
        B: Any,
        C: Any,
        *,
        alpha: Any,
        beta: Any,
        cache_budget_bytes: int,
    ) -> AdaptiveResult:
        A_arr = np.asarray(A)
        B_arr = np.asarray(B)
        if A_arr.ndim != 2 or B_arr.ndim != 2:
            from .errors import GemmError

            raise GemmError(
                "E_SHAPE_MISMATCH",
                "adaptive_gemm expects 2-D A and B",
                {"A_shape": list(A_arr.shape), "B_shape": list(B_arr.shape)},
            )
        M, K = A_arr.shape
        if B_arr.shape[0] != K:
            from .errors import GemmError

            raise GemmError(
                "E_SHAPE_MISMATCH",
                "adaptive_gemm A and B inner dimensions differ",
                {"A_shape": list(A_arr.shape), "B_shape": list(B_arr.shape)},
            )
        N = int(B_arr.shape[1])
        decision = self.select_gemm(
            M,
            N,
            K,
            alpha=alpha,
            beta=beta,
            cache_budget_bytes=cache_budget_bytes,
        )
        output = gemm(
            A_arr,
            B_arr,
            C,
            alpha,
            beta,
            tile=decision["tile"],
            loop_order=decision["loop_order"],
            max_tile_elements=self.runtime.max_tile_elements,
            max_tile_dimension=self.runtime.max_tile_dimension,
        )
        return AdaptiveResult(output=output, decision=decision)

    def adaptive_bmm(
        self,
        A: Any,
        B: Any,
        C: Any,
        *,
        alpha: Any,
        beta: Any,
        cache_budget_bytes: int,
        max_workers: int | None = None,
    ) -> AdaptiveResult:
        A_arr = np.asarray(A)
        B_arr = np.asarray(B)
        if A_arr.ndim != 3 or B_arr.ndim != 3:
            from .errors import GemmError

            raise GemmError(
                "E_SHAPE_MISMATCH",
                "adaptive_bmm expects 3-D A and B",
                {"A_shape": list(A_arr.shape), "B_shape": list(B_arr.shape)},
            )
        batch, M, K = A_arr.shape
        if B_arr.shape[0] != batch or B_arr.shape[1] != K:
            from .errors import GemmError

            raise GemmError(
                "E_SHAPE_MISMATCH",
                "adaptive_bmm A and B shapes are incompatible",
                {"A_shape": list(A_arr.shape), "B_shape": list(B_arr.shape)},
            )
        N = int(B_arr.shape[2])
        decision = self.select_bmm(
            M,
            N,
            K,
            batch=batch,
            alpha=alpha,
            beta=beta,
            cache_budget_bytes=cache_budget_bytes,
        )
        execution = self.execute_candidate_bmm(
            A_arr,
            B_arr,
            C,
            alpha=alpha,
            beta=beta,
            candidate=decision,
            max_workers=max_workers,
        )
        return AdaptiveResult(
            output=execution.output, decision=decision, execution=execution
        )

    def execute_candidate_bmm(
        self,
        A: Any,
        B: Any,
        C: Any,
        *,
        alpha: Any,
        beta: Any,
        candidate: Mapping[str, Any],
        max_workers: int | None = None,
    ) -> BmmExecution:
        workers = self.max_workers if max_workers is None else max_workers
        return execute_bmm(
            A,
            B,
            C,
            alpha,
            beta,
            tile=candidate["tile"],
            loop_order=str(candidate["loop_order"]),
            batch_schedule=str(candidate["batch_schedule"]),
            max_workers=workers,
            max_tile_elements=self.runtime.max_tile_elements,
            max_tile_dimension=self.runtime.max_tile_dimension,
            flatten_chunk_size=self.cost_model.flatten_chunk_size,
            strategy_id=str(candidate["strategy_id"]),
        )

    def cache_stats(self) -> dict[str, int]:
        return self.selector.cache_stats()

