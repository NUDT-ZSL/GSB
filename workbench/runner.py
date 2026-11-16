"""查询集执行：串行与线程池并发，以及逐字段一致性比对。"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from typing import Any, Callable

from .engine import QueryEngine, QueryRequest, QueryResponse
from .errors import E_QUERY_FAILED, make_failure


@dataclass
class QueryRunResult:
    responses: list[QueryResponse]
    mode: str
    max_concurrency: int

    def ordered(self) -> list[QueryResponse]:
        return sorted(self.responses, key=lambda item: item.sort_key)

    def latency_summary(self) -> dict[str, Any]:
        ordered = self.ordered()
        elapsed = [item.elapsed_ms for item in ordered]
        return {
            "mode": self.mode,
            "query_count": len(ordered),
            "max_concurrency": self.max_concurrency,
            "total_elapsed_ms": round(sum(elapsed), 6),
            "max_elapsed_ms": round(max(elapsed), 6) if elapsed else 0.0,
            "min_elapsed_ms": round(min(elapsed), 6) if elapsed else 0.0,
            "budget_exhausted_count": sum(
                1 for item in ordered if item.budget_exhausted
            ),
        }

    def failure_list(self) -> list[dict[str, Any]]:
        failures: list[dict[str, Any]] = []
        for response in self.ordered():
            failures.extend(response.failures)
        return failures

    def to_dict(self) -> dict[str, Any]:
        return {
            "mode": self.mode,
            "max_concurrency": self.max_concurrency,
            "latency_summary": self.latency_summary(),
            "results": [item.to_dict() for item in self.ordered()],
            "failure_list": self.failure_list(),
        }


def run_serial(
    requests: list[QueryRequest],
    engine_factory: Callable[[], QueryEngine],
) -> QueryRunResult:
    engine = engine_factory()
    responses = [engine.run(request) for request in requests]
    return QueryRunResult(responses, "serial", 1)


def run_concurrent(
    requests: list[QueryRequest],
    engine_factory: Callable[[], QueryEngine],
    max_workers: int,
) -> QueryRunResult:
    """每个查询使用独立引擎实例，避免共享可变缓存，按稳定排序键回收结果。"""
    with ThreadPoolExecutor(max_workers=max_workers) as pool:
        futures = {
            pool.submit(engine_factory().run, request): request.sort_key
            for request in requests
        }
        responses: list[QueryResponse] = []
        for future in futures:
            try:
                responses.append(future.result())
            except Exception as exc:  # pragma: no cover - 防御性分支
                sort_key = futures[future]
                request = requests[sort_key]
                placeholder = QueryResponse(
                    query_id=request.query_id,
                    sort_key=sort_key,
                    terms=request.terms,
                )
                placeholder.failures.append(
                    make_failure(request.query_id, "query",
                                 E_QUERY_FAILED, exc).to_dict()
                )
                responses.append(placeholder)
    return QueryRunResult(responses, "concurrent", max_workers)


def compare_query_runs(
    serial: QueryRunResult,
    concurrent: QueryRunResult,
) -> dict[str, Any]:
    """逐字段比较串行与并发结果，耗时字段仅比较预算标注与结构。"""
    serial_responses = serial.ordered()
    concurrent_responses = concurrent.ordered()
    mismatches: list[dict[str, Any]] = []
    if len(serial_responses) != len(concurrent_responses):
        mismatches.append({
            "field": "result_count",
            "serial": len(serial_responses),
            "concurrent": len(concurrent_responses),
        })
    for index, (left, right) in enumerate(
        zip(serial_responses, concurrent_responses)
    ):
        left_canonical = left.canonical()
        right_canonical = right.canonical()
        if left_canonical != right_canonical:
            mismatches.append({
                "field": "canonical_response",
                "sort_key": index,
                "query_id": left.query_id,
                "serial": left_canonical,
                "concurrent": right_canonical,
            })
    if serial.failure_list() != concurrent.failure_list():
        mismatches.append({"field": "failure_list_order_and_content"})
    serial_latency = serial.latency_summary()
    concurrent_latency = concurrent.latency_summary()
    latency_shape_equal = (
        serial_latency["query_count"] == concurrent_latency["query_count"]
        and serial_latency["budget_exhausted_count"]
        == concurrent_latency["budget_exhausted_count"]
    )
    if not latency_shape_equal:
        mismatches.append({"field": "latency_summary_shape"})
    return {
        "equal": not mismatches,
        "compared_queries": len(serial_responses),
        "mismatches": mismatches,
        "latency_serial": serial_latency,
        "latency_concurrent": concurrent_latency,
    }
