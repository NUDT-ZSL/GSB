"""Trace aggregation, deadline checks, and policy comparison."""

from __future__ import annotations

from collections import defaultdict
from typing import Any, Iterable

from .model import Project
from .scheduler import (
    BLOCK_BANDWIDTH,
    BLOCK_CONSUMER_SLOW,
    BLOCK_NONE,
    BLOCK_OUTAGE,
    BLOCK_WINDOW_CAPACITY,
    BLOCK_WINDOW_RTT,
    Scheduler,
)


def percentile(values: Iterable[float], percent: float) -> float | None:
    ordered = sorted(float(value) for value in values)
    if not ordered:
        return None
    if len(ordered) == 1:
        return ordered[0]
    position = (len(ordered) - 1) * percent / 100.0
    lower = int(position)
    upper = min(lower + 1, len(ordered) - 1)
    fraction = position - lower
    return ordered[lower] + (ordered[upper] - ordered[lower]) * fraction


def distribution(values: Iterable[float]) -> dict[str, float | int | None]:
    ordered = [float(value) for value in values]
    if not ordered:
        return {
            "count": 0,
            "mean_ms": None,
            "max_ms": None,
            "p50_ms": None,
            "p95_ms": None,
            "p99_ms": None,
        }
    return {
        "count": len(ordered),
        "mean_ms": sum(ordered) / len(ordered),
        "max_ms": max(ordered),
        "p50_ms": percentile(ordered, 50),
        "p95_ms": percentile(ordered, 95),
        "p99_ms": percentile(ordered, 99),
    }


def analyze(project: Project, scheduler: Scheduler) -> dict[str, Any]:
    records = scheduler.frame_traces()
    sent = [record for record in records if record["status"] == "SENT"]
    unsent = [record for record in records if record["status"] != "SENT"]

    by_stream: dict[str, Any] = {}
    by_pond: dict[str, Any] = {}
    late_commands: list[dict[str, Any]] = []
    timeout_by_stream: dict[str, int] = defaultdict(int)
    all_timeout_count = 0

    for stream in project.streams:
        stream_records = [record for record in records if record["stream_id"] == stream.stream_id]
        stream_sent = [record for record in stream_records if record["status"] == "SENT"]
        timeouts = _timeout_records(stream_records)
        timeout_by_stream[stream.stream_id] = len(timeouts)
        all_timeout_count += len(timeouts)
        if stream.is_control:
            late_commands.extend(timeouts)
        by_stream[stream.stream_id] = {
            "stream_id": stream.stream_id,
            "stream_label": stream.label,
            "pond_id": stream.pond_id,
            "kind": stream.kind,
            "sensor_type": stream.sensor_type,
            "target_device": stream.target_device,
            "priority": stream.priority,
            "window_bytes": stream.window_bytes,
            "deadline_ms": stream.deadline_ms,
            "frame_count": len(stream_records),
            "sent_count": len(stream_sent),
            "unsent_count": len(stream_records) - len(stream_sent),
            "queue_delay": distribution(
                [record["queue_delay_ms"] for record in stream_sent]
            ),
            "delivery_delay": distribution(
                [record["delivery_delay_ms"] for record in stream_sent]
            ),
            "timeout_frames": len(timeouts),
            "on_time": (
                all(record["on_time"] is True for record in stream_records)
                if stream.is_control
                else None
            ),
            "late_frame_ids": [record["frame_id"] for record in timeouts],
        }

    for stream in project.streams:
        pond = by_pond.setdefault(
            stream.pond_id,
            {
                "pond_id": stream.pond_id,
                "streams": [],
                "sent_count": 0,
                "timeout_frames": 0,
            },
        )
        pond["streams"].append(stream.stream_id)
        pond["sent_count"] += by_stream[stream.stream_id]["sent_count"]
        pond["timeout_frames"] += by_stream[stream.stream_id]["timeout_frames"]
    for pond_id, pond in by_pond.items():
        pond_sent = [
            record
            for record in sent
            if record["pond_id"] == pond_id
        ]
        pond["queue_delay"] = distribution(
            [record["queue_delay_ms"] for record in pond_sent]
        )
        pond["delivery_delay"] = distribution(
            [record["delivery_delay_ms"] for record in pond_sent]
        )

    control_streams = [stream for stream in project.streams if stream.is_control]
    blocker_sources = _blocker_source_summary(records)
    result = {
        "frame_count": len(records),
        "sent_count": len(sent),
        "unsent_count": len(unsent),
        "completed_count": scheduler.completed_count,
        "queue_delay": distribution([record["queue_delay_ms"] for record in sent]),
        "delivery_delay": distribution([record["delivery_delay_ms"] for record in sent]),
        "timeout_frames": all_timeout_count,
        "by_stream": by_stream,
        "by_pond": by_pond,
        "control": {
            "control_stream_count": len(control_streams),
            "all_on_time": (
                all(by_stream[stream.stream_id]["on_time"] for stream in control_streams)
                if control_streams
                else True
            ),
            "timeout_count": len(late_commands),
            "late_commands": late_commands,
            "verdicts": {
                stream.stream_id: {
                    "on_time": by_stream[stream.stream_id]["on_time"],
                    "timeout_count": by_stream[stream.stream_id]["timeout_frames"],
                    "deadline_ms": stream.deadline_ms,
                }
                for stream in control_streams
            },
        },
        "blocker_sources": blocker_sources,
        "unsent_frames": [
            {
                "frame_id": record["frame_id"],
                "stream_id": record["stream_id"],
                "status": record["status"],
                "blocking_type": record["blocking_type"],
            }
            for record in unsent
        ],
    }
    return result


def _timeout_records(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    for record in records:
        if record["deadline_ms"] is None:
            continue
        on_time = record.get("on_time")
        if on_time is not True:
            blocked = record.get("blocked_by", {})
            source_stream = blocked.get("stream_id")
            source_frame = blocked.get("frame_id")
            block_type = blocked.get("type") or BLOCK_NONE
            if block_type == BLOCK_NONE:
                source_stream = record["stream_id"]
                source_frame = record["frame_id"]
                block_type = "TRANSFER_TIME"
            result.append(
                {
                    "frame_id": record["frame_id"],
                    "stream_id": record["stream_id"],
                    "pond_id": record["pond_id"],
                    "target_device": record["target_device"],
                    "logical_time_ms": record["logical_time_ms"],
                    "start_ms": record["start_ms"],
                    "end_ms": record["end_ms"],
                    "deadline_at_ms": record["deadline_at_ms"],
                    "delivery_delay_ms": record["delivery_delay_ms"],
                    "blocking_type": block_type,
                    "blocking_stream_id": source_stream,
                    "blocking_frame_id": source_frame,
                    "blocking_description": blocked.get("description"),
                }
            )
    return result


def _blocker_source_summary(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    totals: dict[tuple[str, str | None], float] = defaultdict(float)
    counts: dict[tuple[str, str | None], int] = defaultdict(int)
    frames: dict[tuple[str, str | None], set[str]] = defaultdict(set)
    for record in records:
        for item in record.get("wait_breakdown", []):
            key = (item["type"], item["stream_id"])
            totals[key] += item["duration_ms"]
            counts[key] += 1
            frames[key].add(item["frame_id"] or "")
    result = [
        {
            "type": key[0],
            "source_stream_id": key[1],
            "total_wait_ms": total,
            "segment_count": counts[key],
            "source_frame_count": len([item for item in frames[key] if item]),
        }
        for key, total in totals.items()
        if total > 0
    ]
    result.sort(key=lambda item: (-item["total_wait_ms"], item["type"], item["source_stream_id"] or ""))
    return result


def compare_analyses(
    before_project: Project,
    before_stats: dict[str, Any],
    after_project: Project,
    after_stats: dict[str, Any],
) -> dict[str, Any]:
    streams: dict[str, Any] = {}
    for stream in before_project.streams:
        before = before_stats["by_stream"][stream.stream_id]
        after = after_stats["by_stream"].get(stream.stream_id)
        if after is None:
            continue
        streams[stream.stream_id] = {
            "stream_id": stream.stream_id,
            "stream_label": stream.label,
            "pond_id": stream.pond_id,
            "kind": stream.kind,
            "before": _metric_snapshot(before),
            "after": _metric_snapshot(after),
            "delta_mean_ms": _delta(after["queue_delay"]["mean_ms"], before["queue_delay"]["mean_ms"]),
            "delta_p95_ms": _delta(after["queue_delay"]["p95_ms"], before["queue_delay"]["p95_ms"]),
            "delta_max_ms": _delta(after["queue_delay"]["max_ms"], before["queue_delay"]["max_ms"]),
            "delta_timeouts": after["timeout_frames"] - before["timeout_frames"],
        }

    ponds: dict[str, Any] = {}
    for pond_id, before in before_stats["by_pond"].items():
        after = after_stats["by_pond"].get(pond_id)
        if after is None:
            continue
        ponds[pond_id] = {
            "pond_id": pond_id,
            "before": _metric_snapshot(before),
            "after": _metric_snapshot(after),
            "delta_mean_ms": _delta(after["queue_delay"]["mean_ms"], before["queue_delay"]["mean_ms"]),
            "delta_p95_ms": _delta(after["queue_delay"]["p95_ms"], before["queue_delay"]["p95_ms"]),
            "delta_max_ms": _delta(after["queue_delay"]["max_ms"], before["queue_delay"]["max_ms"]),
            "delta_timeouts": after["timeout_frames"] - before["timeout_frames"],
        }

    policy = {
        "before": {
            "max_concurrent_streams": before_project.network.max_concurrent_streams,
            "priorities": {stream.stream_id: stream.priority for stream in before_project.streams},
            "windows": {stream.stream_id: stream.window_bytes for stream in before_project.streams},
        },
        "after": {
            "max_concurrent_streams": after_project.network.max_concurrent_streams,
            "priorities": {stream.stream_id: stream.priority for stream in after_project.streams},
            "windows": {stream.stream_id: stream.window_bytes for stream in after_project.streams},
        },
    }
    return {
        "policy": policy,
        "streams": streams,
        "ponds": ponds,
        "summary": {
            "before": {
                "mean_queue_delay_ms": before_stats["queue_delay"]["mean_ms"],
                "p95_queue_delay_ms": before_stats["queue_delay"]["p95_ms"],
                "timeout_frames": before_stats["timeout_frames"],
                "late_commands": before_stats["control"]["timeout_count"],
            },
            "after": {
                "mean_queue_delay_ms": after_stats["queue_delay"]["mean_ms"],
                "p95_queue_delay_ms": after_stats["queue_delay"]["p95_ms"],
                "timeout_frames": after_stats["timeout_frames"],
                "late_commands": after_stats["control"]["timeout_count"],
            },
        },
    }


def _metric_snapshot(stats: dict[str, Any]) -> dict[str, Any]:
    return {
        "mean_ms": stats["queue_delay"]["mean_ms"],
        "max_ms": stats["queue_delay"]["max_ms"],
        "p50_ms": stats["queue_delay"]["p50_ms"],
        "p95_ms": stats["queue_delay"]["p95_ms"],
        "p99_ms": stats["queue_delay"]["p99_ms"],
        "timeout_frames": stats["timeout_frames"],
    }


def _delta(after: float | None, before: float | None) -> float | None:
    if after is None or before is None:
        return None
    return after - before


def build_suggestions(
    before_project: Project,
    before_stats: dict[str, Any],
    after_project: Project,
    after_stats: dict[str, Any],
) -> list[dict[str, str]]:
    suggestions: list[dict[str, str]] = []
    sources = before_stats.get("blocker_sources", [])

    bandwidth_sources = [
        item for item in sources if item["type"] == BLOCK_BANDWIDTH and item["source_stream_id"]
    ]
    if bandwidth_sources:
        top = bandwidth_sources[0]
        source_id = top["source_stream_id"]
        source_stream = next(
            (stream for stream in before_project.streams if stream.stream_id == source_id),
            None,
        )
        old_priority = source_stream.priority if source_stream else None
        new_priority = next(
            (stream.priority for stream in after_project.streams if stream.stream_id == source_id),
            old_priority,
        )
        if new_priority != old_priority:
            priority_action = f"将 {source_id}.priority 从 {old_priority} 调为 {new_priority}"
        else:
            priority_action = (
                f"在 priority-order 中将 {source_id} 排到控制/告警流之后"
            )
        suggestions.append(
            {
                "reason": (
                    f"{source_id} 造成 {top['total_wait_ms']:.3f} ms 带宽/并发槽阻塞 "
                    f"({top['segment_count']} 个等待片段)"
                ),
                "action": (
                    f"将 network.max_concurrent_streams 从 "
                    f"{before_project.network.max_concurrent_streams} 调为 "
                    f"{after_project.network.max_concurrent_streams}；"
                    f"{priority_action}，"
                    "让告警/控制流先获得发送槽位"
                ),
                "references": f"{source_id}, network.max_concurrent_streams, priority",
            }
        )

    slow_sources = [
        item for item in sources if item["type"] == BLOCK_CONSUMER_SLOW and item["source_stream_id"]
    ]
    if slow_sources:
        stream_by_id = {stream.stream_id: stream for stream in before_project.streams}
        non_bulk = [
            item
            for item in slow_sources
            if not stream_by_id[item["source_stream_id"]].is_bulk
        ]
        top = non_bulk[0] if non_bulk else slow_sources[0]
        source_id = top["source_stream_id"]
        old_window = next(
            (stream.window_bytes for stream in before_project.streams if stream.stream_id == source_id),
            None,
        )
        new_window = next(
            (stream.window_bytes for stream in after_project.streams if stream.stream_id == source_id),
            old_window,
        )
        source_stream = stream_by_id[source_id]
        if new_window == old_window and old_window is not None:
            new_window = max(int(old_window * 2), max(frame.length_bytes for frame in source_stream.frames))
        suggestions.append(
            {
                "reason": (
                    f"慢消费者排队累积阻塞共 "
                    f"{sum(item['total_wait_ms'] for item in slow_sources):.3f} ms；"
                    f"代表性源流 {source_id} 被窗口/消费积压阻塞 "
                    f"{top['total_wait_ms']:.3f} ms"
                ),
                "action": (
                    f"提高 consumer.read_bytes_per_s；并将 {source_id}.window_bytes "
                    f"从 {old_window} 调为 {new_window}，为在途上报提供足够流控信用"
                ),
                "references": f"{source_id}, window_bytes, consumer.read_bytes_per_s",
            }
        )

    capacity_sources = [
        item
        for item in sources
        if item["type"] == BLOCK_WINDOW_CAPACITY and item["source_stream_id"]
    ]
    if capacity_sources:
        top = capacity_sources[0]
        source_id = top["source_stream_id"]
        old_window = next(
            (stream.window_bytes for stream in before_project.streams if stream.stream_id == source_id),
            None,
        )
        new_window = next(
            (stream.window_bytes for stream in after_project.streams if stream.stream_id == source_id),
            old_window,
        )
        if new_window == old_window and old_window is not None:
            stream = next(
                (item for item in before_project.streams if item.stream_id == source_id),
                None,
            )
            max_frame = max((frame.length_bytes for frame in stream.frames), default=old_window)
            new_window = max(int(old_window * 2), max_frame)
        suggestions.append(
            {
                "reason": (
                    f"{source_id} 的单帧长度超过流窗口，累计阻塞 "
                    f"{top['total_wait_ms']:.3f} ms"
                ),
                "action": (
                    f"将 {source_id}.window_bytes 从 {old_window} 调为 {new_window}，"
                    "至少覆盖该流最大单帧长度"
                ),
                "references": f"{source_id}, window_bytes, frame_size_limit_bytes",
            }
        )

    late = before_stats["control"]["late_commands"]
    if late:
        affected = ", ".join(record["frame_id"] for record in late[:6])
        source_ids = sorted(
            {
                record["blocking_stream_id"]
                for record in late
                if record.get("blocking_stream_id")
            }
        )
        suggestions.append(
            {
                "reason": (
                    f"调整前有 {len(late)} 条控制指令超时：{affected}"
                    + (" …" if len(late) > 6 else "")
                ),
                "action": (
                    "把控制流 "
                    + ", ".join(stream.stream_id for stream in before_project.streams if stream.is_control)
                    + " 置于 priority-order 最前；"
                    + (
                        f"同时针对阻塞源 {', '.join(source_ids)} 调整并发上限/窗口"
                        if source_ids
                        else "并复核对应 deadline_ms"
                    )
                ),
                "references": "priority-order, deadline_ms, " + ", ".join(source_ids),
            }
        )

    outage_records = [item for item in sources if item["type"] == BLOCK_OUTAGE]
    if outage_records:
        suggestions.append(
            {
                "reason": (
                    f"链路瞬断贡献 {sum(item['total_wait_ms'] for item in outage_records):.3f} ms "
                    "不可发送等待"
                ),
                "action": (
                    "瞬断不能靠并发/窗口参数消除；应在控制指令 deadline_ms 中预留链路恢复时间，"
                    "并在本地缓存后按原流内顺序补发"
                ),
                "references": "outages, deadline_ms, stream order",
            }
        )

    if not suggestions:
        suggestions.append(
            {
                "reason": "未发现显著带宽、慢消费者、窗口或瞬断阻塞",
                "action": "维持当前 max_concurrent_streams、priority 与 window_bytes 分配",
                "references": "当前策略",
            }
        )

    before_late = before_stats["control"]["timeout_count"]
    after_late = after_stats["control"]["timeout_count"]
    before_p95 = before_stats["queue_delay"]["p95_ms"]
    after_p95 = after_stats["queue_delay"]["p95_ms"]
    if before_p95 is not None and after_p95 is not None:
        suggestions.append(
            {
                "reason": (
                    f"调整后控制超时从 {before_late} 条变为 {after_late} 条；"
                    f"全局队首阻塞 p95 从 {before_p95:.3f} ms 变为 {after_p95:.3f} ms"
                ),
                "action": (
                    "验收时以逐帧 start_ms/end_ms 和 queue_delay_ms 为准；"
                    "若 p95 未改善，优先回退对非阻塞流的窗口放大"
                ),
                "references": "max_concurrent_streams, priority-order, window_bytes",
            }
        )
    return suggestions


def top_blocker_sources(stats: dict[str, Any], limit: int = 5) -> list[dict[str, Any]]:
    return list(stats.get("blocker_sources", [])[:limit])


def summarize_block_type(value: str) -> str:
    return {
        BLOCK_BANDWIDTH: "带宽/并发",
        BLOCK_WINDOW_RTT: "窗口RTT",
        BLOCK_CONSUMER_SLOW: "慢消费者",
        BLOCK_OUTAGE: "瞬断",
        BLOCK_WINDOW_CAPACITY: "窗口容量",
        "TRANSFER_TIME": "自身发送",
        BLOCK_NONE: "无",
    }.get(value, value)

