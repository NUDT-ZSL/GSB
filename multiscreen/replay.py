from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from typing import Any

from .canon import canonical
from .concurrency import OrderedMemoryGate
from .engine import replay_receiver


def group_events_by_receiver(events: list[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    grouped: dict[str, list[dict[str, Any]]] = {}
    for event in events:
        grouped.setdefault(event["receiver_id"], []).append(event)
    return grouped


def estimate_buffer_bytes(events: list[dict[str, Any]]) -> int:
    return sum(len(canonical(event)) for event in events) + 4096


def run_replay(
    events: list[dict[str, Any]],
    initial_states: dict[str, dict[str, Any]] | None = None,
    *,
    workers: int = 1,
    budget_bytes: int = 64 * 1024 * 1024,
    auto_switch_on_breach: bool = True,
) -> dict[str, Any]:
    """按接收端独立重放；并发只改变调度，不改变归约结果。"""

    grouped = group_events_by_receiver(events)
    receiver_ids = sorted(grouped)
    estimates = {rid: estimate_buffer_bytes(grouped[rid]) for rid in receiver_ids}
    options = {"auto_switch_on_breach": auto_switch_on_breach}
    gate = OrderedMemoryGate(budget_bytes, receiver_ids)

    if workers <= 1 or len(receiver_ids) <= 1:
        results = {}
        for rid in receiver_ids:
            need_bytes = estimates[rid]
            gate.acquire(rid, need_bytes)
            try:
                results[rid] = replay_receiver(
                    rid,
                    grouped[rid],
                    (initial_states or {}).get(rid),
                    **options,
                )
            finally:
                gate.release(rid, need_bytes)
    else:
        def run_one(rid: str) -> tuple[str, dict[str, Any]]:
            need_bytes = estimates[rid]
            gate.acquire(rid, need_bytes)
            try:
                return rid, replay_receiver(
                    rid,
                    grouped[rid],
                    (initial_states or {}).get(rid),
                    **options,
                )
            finally:
                gate.release(rid, need_bytes)

        results = {}
        pool_workers = max(1, min(int(workers), len(receiver_ids)))
        with ThreadPoolExecutor(max_workers=pool_workers) as pool:
            for rid, result in pool.map(run_one, receiver_ids):
                results[rid] = result
        results = dict(sorted(results.items()))

    trace: list[dict[str, Any]] = []
    rebuilds: list[dict[str, Any]] = []
    counts: dict[str, int] = {}
    for rid in receiver_ids:
        result = results[rid]
        trace.extend(result["trace"])
        rebuilds.extend(result["rebuilds"])
        for item in result["trace"]:
            counts[item["decision"]] = counts.get(item["decision"], 0) + 1
    trace.sort(key=lambda item: item["event_seq"])
    rebuilds.sort(key=lambda item: item["trigger_event_seq"])
    return {
        "results": results,
        "trace": trace,
        "rebuilds": rebuilds,
        "counts": dict(sorted(counts.items())),
        "buffer_bytes_by_receiver": estimates,
    }
