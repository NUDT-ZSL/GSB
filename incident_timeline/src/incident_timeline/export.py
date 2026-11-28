"""导出时间线、数量账目、摘要报告，供全量/增量结果比较。"""
from __future__ import annotations

import json
import os

from .storage import latest_run_id, load_snapshot


def export_run(connection, out_dir: str, run_id: int | None = None) -> dict:
    os.makedirs(out_dir, exist_ok=True)
    target_run = run_id or latest_run_id(connection)
    if target_run is None:
        raise RuntimeError("SQLite 中没有可导出的运行结果")
    run_row = connection.execute("SELECT * FROM runs WHERE run_id = ?", (target_run,)).fetchone()
    snapshot = load_snapshot(connection, target_run)

    merge_records = [
        dict(row)
        for row in connection.execute(
            "SELECT * FROM merge_records WHERE run_id = ? ORDER BY step", (target_run,)
        )
    ]
    dedup_records = [
        dict(row)
        for row in connection.execute(
            "SELECT * FROM dedup_records WHERE run_id = ? ORDER BY removed_event_id", (target_run,)
        )
    ]
    changes = [
        {
            **dict(row),
            "previous_fault_id": json.loads(row["detail_json"]).get("previous_fault_id"),
        }
        for row in connection.execute(
            "SELECT * FROM incremental_changes WHERE run_id = ? ORDER BY fault_id", (target_run,)
        )
    ]
    conflicts = [
        json.loads(row["conflict_json"])
        for row in connection.execute(
            "SELECT * FROM timeline_conflicts WHERE run_id = ? ORDER BY seq", (target_run,)
        )
    ]

    payload = {
        "run_id": target_run,
        "mode": run_row["mode"],
        "batch_id": run_row["batch_id"],
        "summary": json.loads(run_row["summary_json"]),
        "timeline": snapshot["timeline"],
        "accounts": {
            "by_source": snapshot["accounts"]["by_source"],
            "by_service": snapshot["accounts"]["by_service"],
        },
        "merge_records": merge_records,
        "dedup_records": dedup_records,
        "incremental_changes": [
            {**change, "detail": json.loads(change.pop("detail_json"))} for change in changes
        ],
        "timeline_conflicts": conflicts,
    }
    json_path = os.path.join(out_dir, f"run_{target_run}_{run_row['mode']}.json")
    with open(json_path, "w", encoding="utf-8") as handle:
        json.dump(payload, handle, ensure_ascii=False, indent=2, sort_keys=True)

    text_path = os.path.join(out_dir, f"run_{target_run}_{run_row['mode']}.txt")
    with open(text_path, "w", encoding="utf-8") as handle:
        handle.write(_render_text(payload))
    return {"json": json_path, "txt": text_path, "run_id": target_run}


def _render_text(payload: dict) -> str:
    lines = []
    lines.append(f"运行编号 run_id={payload['run_id']} 模式={payload['mode']} 批次={payload['batch_id']}")
    summary = payload["summary"]
    lines.append(
        "摘要: 故障 {fault_count} 个（闭环 {closed_fault_count}，未闭环 {open})，"
        "输入 {total_input} 条，重复剔除 {dedup} 条，账目一致 {ok}，时间线无矛盾 {consistent}".format(
            fault_count=summary["fault_count"],
            closed_fault_count=summary["closed_fault_count"],
            open=summary["open_fault_ids"],
            total_input=summary["total_input"],
            dedup=summary["dedup_removed_total"],
            ok=summary["accounting_all_ok"],
            consistent=summary["timeline_consistent"],
        )
    )
    lines.append("")
    lines.append("== 故障时间线 ==")
    for entry in payload["timeline"]:
        end_text = entry["end_ts"] if entry["is_closed"] else f"未闭环(观测最晚 {entry['observed_last_ts']})"
        lines.append(
            f"#{entry['order']} {entry['fault_id']} [{entry['start_ts']} -> {end_text}] "
            f"服务={entry['services']} 影响下游={entry['affected_downstream']}"
        )
        lines.append(f"    影响面: {entry['impact_summary']}")
        for key_event in entry["key_events"]:
            lines.append(
                "    关键事件 {event_id} {source}/{service}/{code}/{status} @ {ts_ms}".format(
                    **key_event
                )
            )
        for basis in entry["key_event_selection_basis"]:
            lines.append(f"      选择依据[{basis['label']}]: {basis['event_id']} - {basis['reason']}")
        if entry["corroborating_event_ids"]:
            lines.append(f"    佐证事件(不去重): {entry['corroborating_event_ids']}")
    lines.append("")
    lines.append("== 合并留痕 ==")
    for record in payload["merge_records"]:
        lines.append(
            f"step {record['step']}: {record['left_candidate']} + {record['right_candidate']} "
            f"依据={record['basis_edge']} gap={record['min_gap_ms']}ms"
        )
    lines.append("")
    lines.append("== 数量账目（按来源） ==")
    for row in payload["accounts"]["by_source"]:
        lines.append(
            f"{row['key']}: 原始 {row['raw_count']} 去重后 {row['deduped_count']} "
            f"剔除 {row['removed_duplicate_count']} 入故障簇 {row['compressed_into_fault_count']} "
            f"佐证 {row['corroborating_count']}"
        )
    lines.append("")
    lines.append("== 数量账目（按服务） ==")
    for row in payload["accounts"]["by_service"]:
        lines.append(
            f"{row['key']}: 原始 {row['raw_count']} 去重后 {row['deduped_count']} "
            f"剔除 {row['removed_duplicate_count']} 入故障簇 {row['compressed_into_fault_count']} "
            f"佐证 {row['corroborating_count']}"
        )
    lines.append("")
    lines.append("== 去重剔除依据 ==")
    for record in payload["dedup_records"]:
        lines.append(
            f"{record['removed_event_id']} -> 保留 {record['kept_event_id']} "
            f"({record['service']}/{record['code']}, gap={record['gap_ms']}ms): {record['reason']}"
        )
    if payload["incremental_changes"]:
        lines.append("")
        lines.append("== 增量变化 ==")
        for change in payload["incremental_changes"]:
            lines.append(f"{change['change_type']}: {change['fault_id']} <- {change['previous_fault_id']}")
    if payload["timeline_conflicts"]:
        lines.append("")
        lines.append("!! 时间线矛盾:")
        for conflict in payload["timeline_conflicts"]:
            lines.append(str(conflict))
    return "\n".join(lines) + "\n"
