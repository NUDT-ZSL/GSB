"""压缩前后数量账目：按来源类型、按服务分别对账，合计必须与输入总条数一致。"""
from __future__ import annotations

from .models import CORROBORATING_SOURCES, DedupRecord, Event, FaultCluster


def build_accounts(
    all_events: list[Event],
    kept_events: list[Event],
    removed_events: list[Event],
    dedup_records: list[DedupRecord],
    faults: list[FaultCluster],
    context_clusters: list[dict],
    unassigned_corroborating: list[dict],
) -> dict:
    total_input = len(all_events)
    removed_ids = {event.event_id for event in removed_events}
    kept_ids = {event.event_id for event in kept_events}

    fault_event_ids: set[str] = set()
    fault_corroborating_ids: set[str] = set()
    per_fault_counts = []
    for fault in faults:
        core_ids = set(fault.event_ids)
        corr_ids = set(fault.corroborating_ids)
        fault_event_ids.update(core_ids)
        fault_corroborating_ids.update(corr_ids)
        per_fault_counts.append(
            {
                "fault_id": fault.fault_id,
                "core_event_count": len(core_ids),
                "corroborating_count": len(corr_ids),
                "total_count": len(core_ids) + len(corr_ids),
            }
        )

    context_event_ids: set[str] = set()
    context_corr_ids: set[str] = set()
    for context in context_clusters:
        context_event_ids.update(context.get("event_ids", []))
        context_corr_ids.update(context.get("corroborating_ids", []))
    unassigned_ids = {item["event_id"] for item in unassigned_corroborating}

    by_source = _dimension(
        all_events,
        kept_ids,
        removed_ids,
        fault_event_ids,
        fault_corroborating_ids,
        context_event_ids,
        context_corr_ids,
        unassigned_ids,
        key_func=lambda event: event.source,
    )
    by_service = _dimension(
        all_events,
        kept_ids,
        removed_ids,
        fault_event_ids,
        fault_corroborating_ids,
        context_event_ids,
        context_corr_ids,
        unassigned_ids,
        key_func=lambda event: event.service,
    )

    reconciliation = _reconcile(
        total_input,
        by_source,
        by_service,
        dedup_records,
        removed_events,
        kept_events,
        fault_event_ids,
        fault_corroborating_ids,
        context_event_ids,
        context_corr_ids,
        unassigned_ids,
    )
    return {
        "total_input": total_input,
        "by_source": by_source,
        "by_service": by_service,
        "per_fault": per_fault_counts,
        "reconciliation": reconciliation,
    }


def _dimension(
    events,
    kept_ids,
    removed_ids,
    fault_event_ids,
    fault_corr_ids,
    context_event_ids,
    context_corr_ids,
    unassigned_ids,
    key_func,
):
    rows = {}
    for event in events:
        key = key_func(event)
        row = rows.setdefault(
            key,
            {
                "key": key,
                "raw_count": 0,
                "deduped_count": 0,
                "removed_duplicate_count": 0,
                "compressed_into_fault_count": 0,
                "context_cluster_count": 0,
                "corroborating_count": 0,
                "unassigned_corroborating_count": 0,
            },
        )
        row["raw_count"] += 1
        is_removed = event.event_id in removed_ids
        if not is_removed:
            row["deduped_count"] += 1
        else:
            row["removed_duplicate_count"] += 1
        if event.event_id in fault_event_ids:
            row["compressed_into_fault_count"] += 1
        if event.event_id in context_event_ids:
            row["context_cluster_count"] += 1
        is_corr = event.source in CORROBORATING_SOURCES and event.event_id in kept_ids
        if is_corr and event.event_id in fault_corr_ids | context_corr_ids:
            row["corroborating_count"] += 1
        if event.event_id in unassigned_ids:
            row["unassigned_corroborating_count"] += 1
    return [rows[key] for key in sorted(rows)]


def _reconcile(
    total_input,
    by_source,
    by_service,
    dedup_records,
    removed_events,
    kept_events,
    fault_event_ids,
    fault_corr_ids,
    context_event_ids,
    context_corr_ids,
    unassigned_ids,
):
    source_raw = sum(row["raw_count"] for row in by_source)
    service_raw = sum(row["raw_count"] for row in by_service)
    removed_count = len(removed_events)
    kept_count = len(kept_events)
    corr_count = len(fault_corr_ids) + len(context_corr_ids)
    explained = (
        len(fault_event_ids)
        + len(fault_corr_ids)
        + len(context_event_ids)
        + len(context_corr_ids)
        + len(unassigned_ids)
        + removed_count
    )
    checks = [
        {"name": "source_raw_equals_input", "ok": source_raw == total_input,
         "detail": f"按来源原始合计 {source_raw} == 输入 {total_input}"},
        {"name": "service_raw_equals_input", "ok": service_raw == total_input,
         "detail": f"按服务原始合计 {service_raw} == 输入 {total_input}"},
        {"name": "kept_plus_removed_equals_input", "ok": kept_count + removed_count == total_input,
         "detail": f"去重保留 {kept_count} + 剔除 {removed_count} == {total_input}"},
        {"name": "every_removal_has_record", "ok": len(dedup_records) == removed_count,
         "detail": f"剔除记录 {len(dedup_records)} 条覆盖 {removed_count} 条被剔除事件"},
        {"name": "all_events_accounted", "ok": explained == total_input,
         "detail": (
             f"故障簇核心 {len(fault_event_ids)} + 故障簇佐证 {len(fault_corr_ids)} + "
             f"上下文簇核心 {len(context_event_ids)} + 上下文簇佐证 {len(context_corr_ids)} + "
             f"未挂载佐证 {len(unassigned_ids)} + 剔除 {removed_count} = {explained} == {total_input}"
         )},
    ]
    for row in by_source:
        delta = row["raw_count"] - row["deduped_count"]
        checks.append(
            {
                "name": f"source_delta_explained:{row['key']}",
                "ok": delta == row["removed_duplicate_count"],
                "detail": (
                    f"来源 {row['key']} 差额 {delta} 全部由重复剔除 "
                    f"{row['removed_duplicate_count']} 解释"
                ),
            }
        )
    for row in by_service:
        delta = row["raw_count"] - row["deduped_count"]
        checks.append(
            {
                "name": f"service_delta_explained:{row['key']}",
                "ok": delta == row["removed_duplicate_count"],
                "detail": (
                    f"服务 {row['key']} 差额 {delta} 全部由重复剔除 "
                    f"{row['removed_duplicate_count']} 解释"
                ),
            }
        )
    all_ok = all(check["ok"] for check in checks)
    return {
        "all_ok": all_ok,
        "checks": checks,
        "corroborating_total": corr_count,
        "dedup_removed_total": removed_count,
        "kept_total": kept_count,
    }
