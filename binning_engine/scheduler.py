"""Deterministic batch splitting and ordered process-pool dispatch."""

from __future__ import annotations

import multiprocessing
import signal
from concurrent.futures import ProcessPoolExecutor
from typing import Any, Callable, Dict, List, Mapping, Optional, Sequence

from .codes import EngineError, ErrorCode
from .encoding import encode_batch_worker


def _worker_parent_death_guard() -> None:
    """Linux/WSL guard so a SIGKILLed parent does not leave pool workers alive."""

    try:
        import ctypes

        libc = ctypes.CDLL("libc.so.6", use_errno=True)
        pr_set_pdeathsig = 1
        libc.prctl(pr_set_pdeathsig, signal.SIGKILL)
    except Exception:
        # The engine still works on platforms without prctl; this is cleanup guard.
        pass


def build_batch_plan(row_ids: Sequence[Any], batch_size: int) -> List[Dict[str, Any]]:
    if batch_size <= 0:
        raise EngineError(ErrorCode.E_RESOURCE_LIMIT, "batch_size must be positive")
    plan: List[Dict[str, Any]] = []
    for index, start in enumerate(range(0, len(row_ids), batch_size)):
        end = min(start + batch_size, len(row_ids))
        plan.append(
            {
                "batch_id": f"batch-{index:05d}",
                "batch_index": index,
                "start_row_position": start,
                "end_row_position": end,
                "row_ids": list(row_ids[start:end]),
            }
        )
    return plan


class BatchScheduler:
    def __init__(self, workers: int = 2, *, use_process_pool: bool = True) -> None:
        if workers <= 0:
            raise EngineError(ErrorCode.E_RESOURCE_LIMIT, "workers must be positive")
        self.workers = int(workers)
        self.use_process_pool = use_process_pool
        self.dispatch_order: List[str] = []
        self.merge_order: List[str] = []

    def run_ordered(
        self,
        tasks: Sequence[Mapping[str, Any]],
        *,
        on_batch_result: Optional[Callable[[Dict[str, Any]], None]] = None,
    ) -> List[Dict[str, Any]]:
        self.dispatch_order = [str(task["batch_id"]) for task in tasks]
        self.merge_order = []
        results: List[Dict[str, Any]] = []

        if not tasks:
            return results

        if not self.use_process_pool:
            for task in tasks:
                result = encode_batch_worker(dict(task))
                self._accept(result, results, on_batch_result)
            return results

        context = multiprocessing.get_context("fork")
        with ProcessPoolExecutor(
            max_workers=self.workers,
            mp_context=context,
            initializer=_worker_parent_death_guard,
        ) as executor:
            future_by_id = {
                str(task["batch_id"]): executor.submit(encode_batch_worker, dict(task))
                for task in tasks
            }
            for batch_id in self.dispatch_order:
                try:
                    result = future_by_id[batch_id].result()
                except Exception as error:
                    result = {
                        "batch_id": batch_id,
                        "ok": False,
                        "batch_error": EngineError(
                            ErrorCode.E_WORKER,
                            f"process-pool worker failed: {error}",
                            batch_id=batch_id,
                        ).to_dict(),
                        "row_results": [],
                        "row_errors": [],
                        "comparison_count": 0,
                    }
                self._accept(result, results, on_batch_result)
        return results

    def _accept(
        self,
        result: Dict[str, Any],
        results: List[Dict[str, Any]],
        on_batch_result: Optional[Callable[[Dict[str, Any]], None]],
    ) -> None:
        batch_id = str(result.get("batch_id"))
        self.merge_order.append(batch_id)
        results.append(result)
        if on_batch_result is not None:
            on_batch_result(result)

    def order_report(self) -> Dict[str, Any]:
        return {
            "workers": self.workers,
            "dispatch_order": list(self.dispatch_order),
            "merge_order": list(self.merge_order),
            "dispatch_matches_merge": self.dispatch_order == self.merge_order,
        }
