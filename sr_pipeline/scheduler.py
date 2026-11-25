"""本地进程池并行调度与双重资源预算准入。

设计要点：

- 查询按输入顺序切成固定大小批次，批次与批内顺序确定性可复现
- 进程池 worker 在初始化时一次性接收索引与配置，任务只传查询
- 候选预算在调度前按批次预估准入，运行期再做防御性累计校验
- 驻留内存按结构计数估算（跨机器稳定），超限直接 E_RESOURCE_LIMIT
- 归并严格按 query_id 输入顺序拼接，与 worker 完成先后无关
"""

from __future__ import annotations

import multiprocessing as mp
from concurrent.futures import ProcessPoolExecutor
from dataclasses import dataclass
from typing import Any

from .config import PipelineConfig
from .errors import BusinessError, ErrorCode
from .fusion import FusionResult, fuse, result_to_jsonable
from .index import SearchIndex, build_index, estimate_index_bytes
from .precision import PrecisionConfig, build_ops
from .recall import run_all_routes
from .selfcheck import check_recomputable, check_route_doc_sets


@dataclass
class QueryOutput:
    """单查询链路产物（worker 返回值）。"""

    query_id: str
    fusion: FusionResult
    route_counts: dict[str, int]
    candidate_count: int


@dataclass
class SchedulerReport:
    """调度与预算裁决报告。"""

    total_queries: int
    batches: list[dict[str, Any]]
    candidate_estimated: int
    candidate_actual: int
    candidate_budget: int
    memory_estimated_bytes: int
    memory_budget_bytes: int
    workers: int
    batch_size: int


def _chunk(items: list, size: int) -> list[list]:
    return [items[start : start + size] for start in range(0, len(items), size)]


def estimate_candidates(queries: list[dict], config: PipelineConfig) -> int:
    """候选预算估算：每查询取各路 cap 之和与 top_k 派生上界的较小值。"""
    caps = sum(route.candidate_cap for route in config.enabled_routes())
    total = 0
    for query in queries:
        total += min(
            caps,
            max(int(query["top_k"]), 1) * len(config.enabled_routes()),
        )
    return total


def admission_check(
    queries: list[dict],
    index: SearchIndex,
    config: PipelineConfig,
) -> tuple[int, int]:
    """调度前双重预算准入，返回 (候选估算, 内存估算)。"""
    candidate_estimate = estimate_candidates(queries, config)
    if candidate_estimate > config.scheduler.candidate_budget:
        raise BusinessError(
            ErrorCode.RECALL_BUDGET_EXCEEDED,
            (
                f"候选预算超限: 估算 {candidate_estimate} > "
                f"上限 {config.scheduler.candidate_budget}"
            ),
            field_path="scheduler.candidate_budget",
            context={
                "estimated": candidate_estimate,
                "budget": config.scheduler.candidate_budget,
            },
        )
    memory_estimate = estimate_index_bytes(index)
    if memory_estimate > config.scheduler.memory_budget_bytes:
        raise BusinessError(
            ErrorCode.RESOURCE_LIMIT,
            (
                f"驻留内存预算超限: 估算 {memory_estimate} 字节 > "
                f"上限 {config.scheduler.memory_budget_bytes} 字节"
            ),
            field_path="scheduler.memory_budget_bytes",
            context={
                "estimated_bytes": memory_estimate,
                "budget_bytes": config.scheduler.memory_budget_bytes,
            },
        )
    return candidate_estimate, memory_estimate


_WORKER_STATE: dict[str, Any] = {}


def _init_worker(
    docs: list[dict],
    queries: list[dict],
    term_weights: dict[str, float],
    config: PipelineConfig,
    precision: PrecisionConfig,
) -> None:
    """每个进程初始化一次索引，避免逐任务重复构建与传输。"""
    ops = build_ops(precision)
    index = build_index(docs, queries, term_weights, config, ops)
    _WORKER_STATE["index"] = index
    _WORKER_STATE["config"] = config
    _WORKER_STATE["ops"] = ops


def _process_query(query: dict) -> QueryOutput:
    index: SearchIndex = _WORKER_STATE["index"]
    config: PipelineConfig = _WORKER_STATE["config"]
    ops = _WORKER_STATE["ops"]
    route_results = run_all_routes(query, index, config, ops)
    check_route_doc_sets(index, route_results)
    fusion_result = fuse(query, route_results, config, ops)
    check_recomputable(fusion_result)
    return QueryOutput(
        query_id=query["query_id"],
        fusion=fusion_result,
        route_counts={result.route: len(result.hits) for result in route_results},
        candidate_count=len(fusion_result.candidates),
    )


def _run_batch(batch: list[dict]) -> list[QueryOutput]:
    return [_process_query(query) for query in batch]


def _run_inline(
    queries: list[dict],
    docs: list[dict],
    term_weights: dict[str, float],
    config: PipelineConfig,
) -> list[QueryOutput]:
    """单进程内联执行路径，workers=1 时使用，同样走完整自检。"""
    _init_worker(docs, queries, term_weights, config, config.precision)
    try:
        return [_process_query(query) for query in queries]
    finally:
        _WORKER_STATE.clear()


def run_scheduler(
    docs: list[dict],
    queries: list[dict],
    term_weights: dict[str, float],
    config: PipelineConfig,
) -> tuple[list[QueryOutput], SchedulerReport]:
    """按批次并行执行召回融合，返回顺序与查询输入顺序一致。"""
    from .validate import validate_config, validate_inputs

    validate_inputs(docs, queries, term_weights, config)
    validate_config(config)
    ops = build_ops(config.precision)
    index = build_index(docs, queries, term_weights, config, ops)
    candidate_estimate, memory_estimate = admission_check(queries, index, config)

    batches = _chunk(queries, config.scheduler.batch_size)
    batch_records: list[dict[str, Any]] = []
    outputs: list[QueryOutput] = []

    if config.scheduler.workers == 1 or len(batches) == 1:
        for batch_index, batch in enumerate(batches):
            batch_outputs = _run_inline(batch, docs, term_weights, config)
            outputs.extend(batch_outputs)
            batch_records.append(
                {
                    "batch_index": batch_index,
                    "query_ids": [query["query_id"] for query in batch],
                    "queries": len(batch),
                    "mode": "inline",
                }
            )
    else:
        ctx = mp.get_context("spawn")
        with ProcessPoolExecutor(
            max_workers=config.scheduler.workers,
            mp_context=ctx,
            initializer=_init_worker,
            initargs=(docs, queries, term_weights, config, config.precision),
        ) as pool:
            futures = [
                (batch_index, pool.submit(_run_batch, batch))
                for batch_index, batch in enumerate(batches)
            ]
            # 按批次下标归并，而非按完成顺序，保证可复现
            for batch_index, future in futures:
                batch_outputs = future.result()
                outputs.extend(batch_outputs)
                batch_records.append(
                    {
                        "batch_index": batch_index,
                        "query_ids": [
                            query["query_id"] for query in batches[batch_index]
                        ],
                        "queries": len(batches[batch_index]),
                        "mode": "process_pool",
                    }
                )

    # 防御性运行期预算校验
    actual_candidates = sum(item.candidate_count for item in outputs)
    if actual_candidates > config.scheduler.candidate_budget:
        raise BusinessError(
            ErrorCode.RECALL_BUDGET_EXCEEDED,
            (
                f"运行期候选预算超限: 实际 {actual_candidates} > "
                f"上限 {config.scheduler.candidate_budget}"
            ),
            field_path="scheduler.candidate_budget",
        )

    report = SchedulerReport(
        total_queries=len(queries),
        batches=batch_records,
        candidate_estimated=candidate_estimate,
        candidate_actual=actual_candidates,
        candidate_budget=config.scheduler.candidate_budget,
        memory_estimated_bytes=memory_estimate,
        memory_budget_bytes=config.scheduler.memory_budget_bytes,
        workers=config.scheduler.workers,
        batch_size=config.scheduler.batch_size,
    )
    return outputs, report


def outputs_to_jsonable(
    outputs: list[QueryOutput], config: PipelineConfig
) -> list[dict]:
    ops = build_ops(config.precision)
    return [result_to_jsonable(item.fusion, ops) for item in outputs]
