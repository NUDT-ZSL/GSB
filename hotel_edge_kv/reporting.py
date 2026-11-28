from __future__ import annotations

import csv
import json
import os
from pathlib import Path
from typing import Any, Iterable, Mapping


def write_json(path: str | Path, data: Any) -> None:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_suffix(target.suffix + ".tmp")
    temporary.write_text(
        json.dumps(data, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    os.replace(temporary, target)


def write_text(path: str | Path, text: str) -> None:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_suffix(target.suffix + ".tmp")
    temporary.write_text(text, encoding="utf-8")
    os.replace(temporary, target)


def write_csv(
    path: str | Path,
    rows: Iterable[Mapping[str, Any]],
    fieldnames: list[str],
) -> None:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_suffix(target.suffix + ".tmp")
    with temporary.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        for row in rows:
            writer.writerow({name: row.get(name) for name in fieldnames})
    os.replace(temporary, target)


def lifecycle_rows(entries: Iterable[Any]) -> list[dict[str, Any]]:
    return [
        {
            "sequence": entry.sequence,
            "time": entry.time,
            "request_id": entry.request_id,
            "ticket_id": entry.ticket_id,
            "event": entry.event,
            "details": json.dumps(entry.details, ensure_ascii=False, sort_keys=True),
        }
        for entry in entries
    ]


def block_ledger_rows(entries: Iterable[Any]) -> list[dict[str, Any]]:
    return [
        {
            "sequence": entry.sequence,
            "time": entry.time,
            "block_id": entry.block_id,
            "event": entry.event,
            "request_id": entry.request_id,
            "ticket_id": entry.ticket_id,
            "reason": entry.reason,
            "details": json.dumps(entry.details, ensure_ascii=False, sort_keys=True),
        }
        for entry in entries
    ]


def build_summary_markdown(summary: Mapping[str, Any]) -> str:
    checks = summary["acceptance"]
    lines = [
        "# 酒店边缘推理 KV 缓存调度摘要",
        "",
        f"- 场景：`{summary['scenario']}`，随机种子：`{summary['seed']}`",
        f"- 逻辑结束时刻：`{summary['scheduler']['current_time']}`",
        f"- 请求：`{summary['scheduler']['requests']['total']}` 条，"
        f"完成 `{summary['scheduler']['requests']['completed']}` 条，"
        f"取消 `{summary['scheduler']['requests']['cancelled']}` 条，"
        f"待处理 `{summary['scheduler']['requests']['pending']}` 条",
        f"- 生成 token：`{summary['scheduler']['tokens_generated']}`",
        f"- 峰值显存：`{summary['scheduler']['peak_memory']['used_bytes']}` / "
        f"`{summary['scheduler']['peak_memory']['budget_bytes']}` 字节",
        "",
        "## 验收核对",
        "",
    ]
    for check in checks["checks"]:
        mark = "通过" if check["passed"] else "失败"
        lines.append(
            f"- `{check['name']}`：{mark}（检查 {check['checked_items']} 项）"
        )
    lines.extend(
        [
            f"- 异常条目：`{checks['issue_count']}`",
            f"- 续算恢复快照一致：`{summary['resume']['snapshot_equal']}`",
            f"- 续算终态与不间断基准一致：`{summary['resume']['matches_reference']}`",
            "",
            "## 取消与回收",
            "",
            f"- 取消记录：`{summary['cancellation']['cancelled_requests']}`",
            f"- 回收块：`{summary['cancellation']['reclaimed_blocks']}`，"
            f"回收字节：`{summary['cancellation']['reclaimed_bytes']}`",
            f"- 回收时延最值：`{summary['cancellation']['latency_ticks']['min']}` / "
            f"`{summary['cancellation']['latency_ticks']['max']}` tick",
            f"- 即时回收率：`{summary['cancellation']['immediate_reclaim_rate']}`",
            f"- 同刻竞争裁决：`{summary['scheduler']['competition_records']}` 条",
            f"- 被取消请求让出的块再次分配："
            f"`{summary['scheduler']['blocks']['reused_blocks_from_cancellations']}` 次",
            "",
            "## 结果文件",
            "",
            "- `ledger/lifecycle.json` / `lifecycle.csv`：逐请求完整生命周期",
            "- `ledger/block_ledger.json` / `block_ledger.csv`：逐块分配、回收与复用来源",
            "- `reports/cancellation_report.json`：取消原因、阶段、时延与效率统计",
            "- `reports/competition_report.json`：同刻取消/完成及多次取消裁决",
            "- `reports/memory_timeline.json`：逐逻辑时刻显存占用曲线",
            "- `reports/audit_report.json`：可定位到块与工单的核对报告",
            "- `reports/resume_report.json`：续算前后状态与基准一致性",
            "- `checkpoint/resume_checkpoint.json`：持久化续算点",
            "",
        ]
    )
    return "\n".join(lines)
