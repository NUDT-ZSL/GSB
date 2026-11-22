from __future__ import annotations

from typing import Any, Iterable


def eligible_streams(streams: dict[str, dict[str, Any]], connection: dict[str, Any]) -> list[dict[str, Any]]:
    if connection.get("goaway", {}).get("last_stream_id") is not None:
        last = connection["goaway"]["last_stream_id"]
    else:
        last = None
    result: list[dict[str, Any]] = []
    for stream in streams.values():
        if stream.get("reset"):
            continue
        if stream.get("local_end") or stream.get("remote_end"):
            # A remote half-close still permits local sends.
            if stream.get("local_end"):
                continue
        if stream.get("state") not in {"open", "half-closed-remote"}:
            continue
        if last is not None and stream["stream_id"] > last:
            continue
        if stream.get("send_window", 0) <= 0:
            continue
        result.append(stream)
    return result


def compute_ranks(
    streams: dict[str, dict[str, Any]],
    connection: dict[str, Any],
) -> list[dict[str, Any]]:
    active = eligible_streams(streams, connection)
    active.sort(key=lambda item: (-item["weight"], item["stream_id"]))
    ranks: list[dict[str, Any]] = []
    for index, stream in enumerate(active, start=1):
        ranks.append(
            {
                "rank": index,
                "stream_id": stream["stream_id"],
                "weight": stream["weight"],
                "send_window": stream["send_window"],
            }
        )
    return ranks


def apply_ranks(state: dict[str, Any]) -> None:
    ranks = compute_ranks(state["streams"], state["connection"])
    rank_by_stream = {item["stream_id"]: item["rank"] for item in ranks}
    for stream in state["streams"].values():
        stream["schedule_rank"] = rank_by_stream.get(stream["stream_id"])
    state["scheduler"]["ranks"] = ranks
    state["scheduler"]["plan"] = weighted_plan(state, max_steps=12)


def weighted_plan(state: dict[str, Any], max_steps: int = 12) -> list[dict[str, Any]]:
    """Deterministic smooth weighted round-robin preview.

    This is a projection for future dispatch only. Committed DATA order lives
    in ``scheduler.committed_order`` and is never rewritten by a weight patch.
    """
    active = eligible_streams(state["streams"], state["connection"])
    if not active or max_steps <= 0:
        return []
    current = {stream["stream_id"]: 0 for stream in active}
    total = sum(stream["weight"] for stream in active)
    remaining_by_stream = {
        stream["stream_id"]: int(stream["send_window"]) for stream in active
    }
    quantum = int(state.get("settings", {}).get("peer", {}).get("max_frame_size", 16384))
    remaining_connection = int(state["connection"]["send_window"])
    plan: list[dict[str, Any]] = []
    for _ in range(max_steps):
        if remaining_connection <= 0:
            break
        for stream in active:
            current[stream["stream_id"]] += stream["weight"]
        chosen = max(
            active,
            key=lambda stream: (current[stream["stream_id"]], -stream["stream_id"]),
        )
        current[chosen["stream_id"]] -= total
        allocation = min(
            quantum,
            remaining_connection,
            remaining_by_stream[chosen["stream_id"]],
        )
        if allocation <= 0:
            break
        plan.append(
            {
                "stream_id": chosen["stream_id"],
                "weight": chosen["weight"],
                "bytes": allocation,
                "virtual_finish": current[chosen["stream_id"]],
            }
        )
        remaining_connection -= allocation
        remaining_by_stream[chosen["stream_id"]] -= allocation
        if remaining_by_stream[chosen["stream_id"]] <= 0:
            active = [item for item in active if item["stream_id"] != chosen["stream_id"]]
            total = sum(item["weight"] for item in active)
            if not active:
                break
    return plan


def stable_rank_map(ranks: Iterable[dict[str, Any]]) -> dict[int, int]:
    return {int(item["stream_id"]): int(item["rank"]) for item in ranks}
