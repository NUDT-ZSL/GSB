from __future__ import annotations

import threading
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from dataclasses import dataclass
from typing import Any, Callable, Iterable

from .errors import ResourceLimitError


@dataclass(frozen=True)
class WorkChunk:
    index: int
    payload: Any
    estimated_bytes: int = 1


@dataclass
class ChunkFailure:
    index: int
    error: BaseException


def run_bounded_chunks(
    chunks: Iterable[WorkChunk],
    worker: Callable[[WorkChunk, threading.Event], Any],
    *,
    max_workers: int = 4,
    memory_budget_bytes: int = 64 * 1024 * 1024,
) -> list[Any]:
    """Run chunks in a bounded pool while reserving in-flight result memory."""

    if max_workers < 1:
        raise ValueError("max_workers must be >= 1")
    if memory_budget_bytes < 1:
        raise ValueError("memory_budget_bytes must be >= 1")

    pending: list[WorkChunk] = list(chunks)
    for chunk in pending:
        if chunk.estimated_bytes > memory_budget_bytes:
            raise ResourceLimitError(
                "chunk exceeds in-flight memory budget",
                details={
                    "chunk_index": chunk.index,
                    "estimated_bytes": chunk.estimated_bytes,
                    "memory_budget_bytes": memory_budget_bytes,
                },
            )

    results: list[Any] = [None] * len(pending)
    cancel_event = threading.Event()
    executor = ThreadPoolExecutor(max_workers=max_workers, thread_name_prefix="narrative-precompute")
    futures: dict[Any, WorkChunk] = {}
    inflight_bytes = 0
    failure: ChunkFailure | None = None

    def submit_next() -> bool:
        nonlocal inflight_bytes
        if failure is not None or not pending:
            return False
        chunk = pending[0]
        if len(futures) >= max_workers:
            return False
        if inflight_bytes + chunk.estimated_bytes > memory_budget_bytes:
            return False
        pending.pop(0)
        inflight_bytes += chunk.estimated_bytes
        future = executor.submit(worker, chunk, cancel_event)
        futures[future] = chunk
        return True

    try:
        while pending or futures:
            if failure is not None:
                break

            while submit_next():
                pass

            if not futures:
                if pending:
                    # No future occupies budget here; oversized chunks were
                    # rejected above, so this guard prevents a busy loop.
                    candidate = pending[0]
                    raise ResourceLimitError(
                        "unable to schedule chunk under memory budget",
                        details={"chunk_index": candidate.index},
                    )
                break

            done, _ = wait(set(futures), return_when=FIRST_COMPLETED)
            for future in done:
                chunk = futures.pop(future)
                inflight_bytes -= chunk.estimated_bytes
                try:
                    result = future.result()
                except BaseException as exc:
                    if failure is None or chunk.index < failure.index:
                        failure = ChunkFailure(index=chunk.index, error=exc)
                    cancel_event.set()
                else:
                    if failure is None:
                        results[chunk.index] = result

            if failure is not None:
                for future in list(futures):
                    future.cancel()
                break

        if failure is not None:
            for future in list(futures):
                future.cancel()
            raise failure.error
        return results
    finally:
        executor.shutdown(wait=False, cancel_futures=True)
