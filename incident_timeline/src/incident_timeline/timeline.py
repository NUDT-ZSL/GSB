"""按逻辑时刻生成故障时间线，并做时间倒序/范围重叠矛盾检查。"""
from __future__ import annotations

from .models import Event, FaultCluster


def build_timeline(
    faults: list[FaultCluster], event_index: dict[str, Event]
) -> dict:
    ordered = sorted(faults, key=lambda cluster: (cluster.start_ts, cluster.fault_id))
    entries = []
    for order, fault in enumerate(ordered, start=1):
        entries.append(_entry(order, fault, event_index))
    conflicts = _check_conflicts(ordered)
    return {
        "entries": entries,
        "conflicts": conflicts,
        "consistent": not conflicts,
    }


def _entry(order: int, fault: FaultCluster, event_index: dict[str, Event]) -> dict:
    cluster_events = [event_index[event_id] for event_id in fault.event_ids if event_id in event_index]
    cluster_events.sort(key=lambda item: (item.ts_ms, item.event_id))
    key_events, selection_basis = _select_key_events(fault, cluster_events)
    end_display = fault.end_ts if fault.is_closed else None
    return {
        "order": order,
        "fault_id": fault.fault_id,
        "start_ts": fault.start_ts,
        "end_ts": end_display,
        "observed_last_ts": fault.end_ts,
        "is_closed": fault.is_closed,
        "open_triggers": fault.open_triggers,
        "services": fault.services,
        "candidate_ids": fault.candidate_ids,
        "impact_summary": fault.impact_summary,
        "affected_downstream": fault.affected_downstream,
        "not_on_dependency_chain": fault.not_on_dependency_chain,
        "root_cause_ranking": fault.root_cause_ranking,
        "key_events": key_events,
        "key_event_selection_basis": selection_basis,
        "corroborating_event_ids": fault.corroborating_ids,
        "boundary_basis": fault.boundary_basis,
        "changed_vs_previous": fault.changed_vs_previous,
    }


def _select_key_events(fault: FaultCluster, cluster_events: list[Event]):
    picked: dict[str, Event] = {}
    basis: list[dict] = []

    def add(label: str, event: Event | None, reason: str):
        if event is None or event.event_id in picked:
            if event is not None and event.event_id in picked:
                basis.append(
                    {"label": label, "event_id": event.event_id,
                     "reason": reason + "（与已选关键事件相同，不重复列出）"}
                )
            return
        picked[event.event_id] = event
        basis.append({"label": label, "event_id": event.event_id, "reason": reason})

    anomalies = [
        event for event in cluster_events
        if event.source in ("alert", "probe") and event.status == "trigger"
    ]
    first_anomaly = min(anomalies, key=lambda item: (item.ts_ms, item.event_id)) if anomalies else None
    add("first_anomaly", first_anomaly, "簇内最早的告警/探测触发，用于定位故障起点与首要根因候选")

    if fault.root_cause_ranking:
        root_service = fault.root_cause_ranking[0]["service"]
        root_events = [
            event for event in cluster_events
            if event.service == root_service
            and event.source in ("alert", "probe")
            and event.status == "trigger"
        ]
        root_first = min(root_events, key=lambda item: (item.ts_ms, item.event_id)) if root_events else None
        add("root_service_first_anomaly", root_first,
            f"根因排序第一服务 {root_service} 的最早异常，用于根因判定")

    releases = [event for event in cluster_events if event.source == "release"]
    first_release = min(releases, key=lambda item: (item.ts_ms, item.event_id)) if releases else None
    add("first_release", first_release, "窗口内最早发布事件，用于判断故障与变更的相关性")
    rollback = next(
        (event for event in sorted(releases, key=lambda item: (item.ts_ms, item.event_id))
         if "rollback" in event.code or event.status == "rollback"),
        None,
    )
    add("rollback", rollback, "回滚事件，用于刻画处置动作")

    recoveries = [
        event for event in cluster_events
        if event.source in ("alert", "probe") and event.status == "recovery"
    ]
    last_recovery = max(recoveries, key=lambda item: (item.ts_ms, item.event_id)) if recoveries else None
    add("last_recovery", last_recovery, "最晚恢复事件，用于修正故障结束时刻")

    key_events = []
    for event in sorted(picked.values(), key=lambda item: (item.ts_ms, item.event_id)):
        key_events.append(
            {
                "event_id": event.event_id,
                "source": event.source,
                "service": event.service,
                "code": event.code,
                "status": event.status,
                "ts_ms": event.ts_ms,
            }
        )
    return key_events, basis


def _check_conflicts(ordered_faults: list[FaultCluster]) -> list[dict]:
    conflicts: list[dict] = []
    previous: FaultCluster | None = None
    for fault in ordered_faults:
        if fault.end_ts is not None and fault.end_ts < fault.start_ts:
            conflicts.append(
                {"type": "range_inverted", "fault_id": fault.fault_id,
                 "reason": f"结束 {fault.end_ts} 早于开始 {fault.start_ts}"}
            )
        if previous is not None:
            if fault.start_ts < previous.start_ts:
                conflicts.append(
                    {"type": "order_violation", "fault_id": fault.fault_id,
                     "reason": f"开始时刻 {fault.start_ts} 早于上一故障 {previous.fault_id} 的 {previous.start_ts}"}
                )
            previous_end = previous.end_ts if previous.end_ts is not None else previous.start_ts
            current_end = fault.end_ts if fault.end_ts is not None else fault.start_ts
            if fault.start_ts < previous_end:
                conflicts.append(
                    {
                        "type": "range_overlap",
                        "fault_id": fault.fault_id,
                        "other_fault_id": previous.fault_id,
                        "reason": (
                            f"范围重叠：{previous.fault_id}[{previous.start_ts},{previous_end}] 与 "
                            f"{fault.fault_id}[{fault.start_ts},{current_end}]"
                        ),
                    }
                )
        previous = fault
    return conflicts
