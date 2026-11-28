"""Human-readable terminal reports."""

from __future__ import annotations

from typing import Any, Iterable

from .analysis import summarize_block_type
from .model import PROJECT_FORMAT, PROJECT_VERSION, Project
from .scheduler import SNAPSHOT_VERSION, Scheduler


def _fmt(value: Any, digits: int = 3) -> str:
    if value is None:
        return "-"
    if isinstance(value, float):
        text = f"{value:.{digits}f}"
        return text.rstrip("0").rstrip(".") if "." in text else text
    return str(value)


def _table(headers: list[str], rows: Iterable[Iterable[Any]], max_widths: list[int] | None = None) -> str:
    rendered = [[str(cell) for cell in row] for row in rows]
    widths = [len(header) for header in headers]
    for row in rendered:
        for index, cell in enumerate(row):
            widths[index] = max(widths[index], len(cell))
    if max_widths:
        widths = [min(width, maximum) for width, maximum in zip(widths, max_widths)]

    def fit(cell: str, width: int) -> str:
        if len(cell) > width:
            return cell[: max(0, width - 1)] + "…"
        return cell.ljust(width)

    lines = ["  ".join(fit(header, widths[index]) for index, header in enumerate(headers))]
    lines.append("  ".join("-" * width for width in widths))
    for row in rendered:
        lines.append("  ".join(fit(cell, widths[index]) for index, cell in enumerate(row)))
    return "\n".join(lines)


def format_project(project: Project) -> str:
    lines = [
        f"工程: {project.name}",
        (
            f"网络: 上行 {_fmt(project.network.uplink_bytes_per_s)} B/s, "
            f"RTT {_fmt(project.network.rtt_ms)} ms, "
            f"max_concurrent_streams={project.network.max_concurrent_streams}, "
            f"frame_size_limit_bytes={project.network.frame_size_limit_bytes}"
        ),
        (
            f"消费者: 基准读取 {_fmt(project.consumer.read_bytes_per_s)} B/s"
            + (
                "；速率事件 "
                + ", ".join(
                    f"{_fmt(event.time_ms)}ms→{_fmt(event.read_bytes_per_s)}B/s"
                    for event in project.consumer.events
                )
                if project.consumer.events
                else ""
            )
        ),
        (
            "链路瞬断: "
            + (
                ", ".join(
                    f"{_fmt(outage.start_ms)}-{_fmt(outage.end_ms)}ms"
                    for outage in project.outages
                )
                if project.outages
                else "无"
            )
        ),
        f"流数量: {len(project.streams)}；帧数量: {project.total_frames}",
        "",
        "流标识：",
    ]
    rows = []
    for stream in project.streams:
        role = "控制指令"
        if stream.is_sensor:
            role = f"传感器/{stream.sensor_type}"
        elif stream.is_bulk:
            role = f"历史大块/{stream.sensor_type or '-'}"
        rows.append(
            [
                stream.stream_id,
                stream.pond_id,
                role,
                _fmt(stream.priority),
                _fmt(stream.window_bytes),
                _fmt(stream.deadline_ms),
                len(stream.frames),
                stream.target_device or "-",
            ]
        )
    lines.append(
        _table(
            ["stream_id", "塘口", "类型", "优先级", "窗口B", "时限ms", "帧数", "目的设备"],
            rows,
            [28, 7, 30, 8, 9, 9, 6, 24],
        )
    )
    return "\n".join(lines)


def format_schedule(project: Project, scheduler: Scheduler, stats: dict[str, Any]) -> str:
    lines = [format_project(project), "", "逐帧调度轨迹："]
    rows = []
    for record in scheduler.frame_traces():
        blocked = record["blocked_by"]
        rows.append(
            [
                record["stream_id"],
                record["frame_id"],
                _fmt(record["logical_time_ms"]),
                _fmt(record["start_ms"]),
                _fmt(record["end_ms"]),
                _fmt(record["queue_delay_ms"]),
                summarize_block_type(record["blocking_type"]),
                blocked.get("stream_id") or "-",
                blocked.get("frame_id") or "-",
                _fmt(record.get("outage_total_ms", 0.0)),
                record["status"],
            ]
        )
    lines.append(
        _table(
            [
                "流",
                "帧",
                "就绪ms",
                "开始ms",
                "完成ms",
                "队首ms",
                "阻塞类型",
                "阻塞源流",
                "阻塞源帧",
                "瞬断ms",
                "状态",
            ],
            rows,
            [22, 26, 10, 11, 11, 10, 13, 22, 26, 8, 12],
        )
    )
    lines.extend(["", format_stats(stats)])
    return "\n".join(lines)


def format_stats(stats: dict[str, Any]) -> str:
    lines = [
        "时延统计（队首阻塞 = start_ms - 就绪时刻）：",
        (
            f"总体: 帧 {stats['frame_count']}，已发 {stats['sent_count']}，"
            f"未发 {stats['unsent_count']}，均值 {_fmt(stats['queue_delay']['mean_ms'])} ms，"
            f"最大 {_fmt(stats['queue_delay']['max_ms'])} ms，"
            f"p50/p95/p99 "
            f"{_fmt(stats['queue_delay']['p50_ms'])}/"
            f"{_fmt(stats['queue_delay']['p95_ms'])}/"
            f"{_fmt(stats['queue_delay']['p99_ms'])} ms"
        ),
        "",
        "逐流：",
    ]
    rows = []
    for stream_id, item in stats["by_stream"].items():
        q = item["queue_delay"]
        rows.append(
            [
                stream_id,
                item["pond_id"],
                item["kind"],
                item["sent_count"],
                _fmt(q["mean_ms"]),
                _fmt(q["max_ms"]),
                _fmt(q["p50_ms"]),
                _fmt(q["p95_ms"]),
                _fmt(q["p99_ms"]),
                item["timeout_frames"],
            ]
        )
    lines.append(
        _table(
            ["流", "塘口", "类型", "已发", "均值", "最大", "p50", "p95", "p99", "超时"],
            rows,
            [22, 7, 12, 6, 9, 9, 9, 9, 9, 6],
        )
    )
    lines.extend(["", "逐塘口："])
    pond_rows = []
    for pond_id, item in stats["by_pond"].items():
        q = item["queue_delay"]
        pond_rows.append(
            [
                pond_id,
                item["sent_count"],
                _fmt(q["mean_ms"]),
                _fmt(q["max_ms"]),
                _fmt(q["p50_ms"]),
                _fmt(q["p95_ms"]),
                _fmt(q["p99_ms"]),
                item["timeout_frames"],
            ]
        )
    lines.append(
        _table(
            ["塘口", "已发", "均值", "最大", "p50", "p95", "p99", "超时"],
            pond_rows,
            [7, 6, 9, 9, 9, 9, 9, 6],
        )
    )

    control = stats["control"]
    lines.extend(["", "控制指令送达判定："])
    if not control["verdicts"]:
        lines.append("无控制指令流")
    else:
        control_rows = []
        for stream_id, verdict in control["verdicts"].items():
            control_rows.append(
                [
                    stream_id,
                    _fmt(verdict["deadline_ms"]),
                    "按时" if verdict["on_time"] else "超时",
                    verdict["timeout_count"],
                ]
            )
        lines.append(_table(["控制流", "时限ms", "判定", "超时数"], control_rows))
        if control["late_commands"]:
            lines.append("超时指令清单：")
            late_rows = []
            for item in control["late_commands"]:
                late_rows.append(
                    [
                        item["frame_id"],
                        item["stream_id"],
                        _fmt(item["deadline_at_ms"]),
                        _fmt(item["end_ms"]),
                        summarize_block_type(item["blocking_type"]),
                        item["blocking_stream_id"] or "-",
                        item["blocking_frame_id"] or "-",
                    ]
                )
            lines.append(
                _table(
                    ["帧", "流", "截止ms", "完成ms", "阻塞类型", "阻塞源流", "阻塞源帧"],
                    late_rows,
                    [26, 22, 11, 11, 13, 22, 26],
                )
            )
        else:
            lines.append("超时指令清单：空")

    lines.extend(["", "阻塞源汇总（按时长排序）："])
    sources = stats["blocker_sources"]
    if sources:
        rows = []
        for item in sources[:12]:
            rows.append(
                [
                    summarize_block_type(item["type"]),
                    item["source_stream_id"] or "LINK",
                    _fmt(item["total_wait_ms"]),
                    item["segment_count"],
                    item["source_frame_count"],
                ]
            )
        lines.append(
            _table(
                ["类型", "源流", "累计等待ms", "片段数", "源帧数"],
                rows,
                [13, 26, 12, 8, 8],
            )
        )
    else:
        lines.append("无")
    return "\n".join(lines)


def format_compare(comparison: dict[str, Any], suggestions: list[dict[str, str]]) -> str:
    before = comparison["summary"]["before"]
    after = comparison["summary"]["after"]
    lines = [
        "策略调整前后对比：",
        (
            f"总体: 均值 {_fmt(before['mean_queue_delay_ms'])}→"
            f"{_fmt(after['mean_queue_delay_ms'])} ms；"
            f"p95 {_fmt(before['p95_queue_delay_ms'])}→"
            f"{_fmt(after['p95_queue_delay_ms'])} ms；"
            f"控制超时 {before['late_commands']}→{after['late_commands']} 条"
        ),
        "",
        "逐塘口：",
    ]
    rows = []
    for pond_id, item in comparison["ponds"].items():
        rows.append(
            [
                pond_id,
                _fmt(item["before"]["mean_ms"]),
                _fmt(item["after"]["mean_ms"]),
                _fmt(item["delta_mean_ms"]),
                _fmt(item["before"]["p50_ms"]),
                _fmt(item["after"]["p50_ms"]),
                _fmt(item["before"]["p95_ms"]),
                _fmt(item["after"]["p95_ms"]),
                _fmt(item["delta_p95_ms"]),
                _fmt(item["before"]["p99_ms"]),
                _fmt(item["after"]["p99_ms"]),
                _fmt(item["before"]["max_ms"]),
                _fmt(item["after"]["max_ms"]),
                f"{item['before']['timeout_frames']}→{item['after']['timeout_frames']}",
            ]
        )
    lines.append(
        _table(
            [
                "塘口",
                "均值前",
                "均值后",
                "Δ均值",
                "p50前",
                "p50后",
                "p95前",
                "p95后",
                "Δp95",
                "p99前",
                "p99后",
                "最大前",
                "最大后",
                "超时",
            ],
            rows,
            [7, 9, 9, 9, 9, 9, 9, 9, 9, 9, 9, 9, 9, 10],
        )
    )
    lines.extend(["", "逐流："])
    stream_rows = []
    for stream_id, item in comparison["streams"].items():
        stream_rows.append(
            [
                stream_id,
                item["pond_id"],
                _fmt(item["before"]["mean_ms"]),
                _fmt(item["after"]["mean_ms"]),
                _fmt(item["delta_mean_ms"]),
                _fmt(item["before"]["p50_ms"]),
                _fmt(item["after"]["p50_ms"]),
                _fmt(item["before"]["p95_ms"]),
                _fmt(item["after"]["p95_ms"]),
                _fmt(item["delta_p95_ms"]),
                _fmt(item["before"]["p99_ms"]),
                _fmt(item["after"]["p99_ms"]),
                _fmt(item["before"]["max_ms"]),
                _fmt(item["after"]["max_ms"]),
                f"{item['before']['timeout_frames']}→{item['after']['timeout_frames']}",
            ]
        )
    lines.append(
        _table(
            [
                "流",
                "塘口",
                "均值前",
                "均值后",
                "Δ均值",
                "p50前",
                "p50后",
                "p95前",
                "p95后",
                "Δp95",
                "p99前",
                "p99后",
                "最大前",
                "最大后",
                "超时",
            ],
            stream_rows,
            [22, 7, 9, 9, 9, 9, 9, 9, 9, 9, 9, 9, 9, 9, 10],
        )
    )
    lines.extend(["", "优化建议："])
    for index, suggestion in enumerate(suggestions, start=1):
        lines.append(f"{index}. 依据: {suggestion['reason']}")
        lines.append(f"   调整: {suggestion['action']}")
        lines.append(f"   引用: {suggestion['references']}")
    return "\n".join(lines)


def format_snapshot_summary(
    project: Project,
    scheduler: Scheduler,
    snapshot_path: str,
    verify_result: dict[str, Any] | None = None,
) -> str:
    lines = [
        f"快照: {snapshot_path}",
        f"版本: {SNAPSHOT_VERSION}",
        f"工程: {project.name}",
        f"调度时刻: {_fmt(scheduler.now_ms)} ms",
        f"已完成帧: {scheduler.completed_count}",
        f"在途帧: {sum(1 for runtime in scheduler.runtime.values() if runtime.active is not None)}",
        f"未归还信用: {sum(1 for credit in scheduler.credits if not credit.returned)}",
    ]
    if verify_result is not None:
        lines.extend(
            [
                "",
                "续算一致性: "
                + ("逐帧完全一致" if verify_result["identical"] else "存在差异"),
                (
                    f"比较帧数: {verify_result['compared_frames']}，"
                    f"差异帧数: {verify_result['different_frames']}"
                ),
            ]
        )
        if verify_result.get("first_difference"):
            lines.append(f"首个差异: {verify_result['first_difference']}")
    return "\n".join(lines)


def project_header(project: Project) -> dict[str, Any]:
    return {
        "format": PROJECT_FORMAT,
        "project_version": PROJECT_VERSION,
        "name": project.name,
        "network": project.network.to_dict(),
        "stream_count": len(project.streams),
        "frame_count": project.total_frames,
    }
