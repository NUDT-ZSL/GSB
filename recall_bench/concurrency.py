"""Stable-key thread pool execution.

Tasks carry an explicit stable key. Results are always returned sorted by that
key, regardless of thread completion order, guaranteeing byte-identical output
to a single-threaded run.
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from typing import Any, Callable


def run_stable(
    tasks: list[tuple[str, Callable[[], Any]]],
    max_workers: int,
) -> list[tuple[str, Any]]:
    ordered_tasks = sorted(tasks, key=lambda pair: pair[0])
    if max_workers <= 1 or len(ordered_tasks) <= 1:
        return [(key, func()) for key, func in ordered_tasks]
    with ThreadPoolExecutor(max_workers=max_workers) as pool:
        futures = {pool.submit(func): key for key, func in ordered_tasks}
        collected: list[tuple[str, Any]] = []
        for future in futures:
            collected.append((futures[future], future.result()))
    collected.sort(key=lambda pair: pair[0])
    return collected
