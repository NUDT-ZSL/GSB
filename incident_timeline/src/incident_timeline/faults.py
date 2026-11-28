"""故障簇装配：边界判定、根因候选、影响面、佐证事件。"""
from __future__ import annotations

from dataclasses import dataclass

from .clustering import Candidate
from .models import CORROBORATING_SOURCES, Event, FaultCluster
from .registry import DependencyRegistry


@dataclass
class FaultBuildResult:
    faults: list[FaultCluster]
    context_clusters: list[dict]
    unassigned_corroborating: list[dict]


def build_faults(
    groups: dict[str, list[Candidate]],
    events: list[Event],
    registry: DependencyRegistry,
    corroborating_tolerance_ms: int = 0,
) -> FaultBuildResult:
    event_index = {event.event_id: event for event in events}
    faults: list[FaultCluster] = []
    context_clusters: list[dict] = []
    fault_seq = 0
    context_seq = 0

    for root, members in groups.items():
        member_events = [event for candidate in members for event in candidate.events]
        member_events.sort(key=lambda item: (item.ts_ms, item.event_id))
        anomaly_events = [
            event
            for event in member_events
            if event.source in ("alert", "probe") and event.status == "trigger"
        ]
        candidate_ids = [candidate.candidate_id for candidate in members]
        services = sorted({candidate.service for candidate in members})

        if not anomaly_events:
            context_seq += 1
            context_clusters.append(
                {
                    "context_id": f"CTX-{context_seq:03d}",
                    "candidate_ids": candidate_ids,
                    "event_ids": [event.event_id for event in member_events],
                    "services": services,
                    "start_ts": min(event.ts_ms for event in member_events),
                    "end_ts": max(event.ts_ms for event in member_events),
                    "reason": "簇内没有异常触发（仅发布/回滚或恢复事件），不计为故障簇",
                }
            )
            continue

        fault_seq += 1
        end_ts, is_closed, open_triggers, boundary_basis = _resolve_boundary(member_events)
        ranking, release_notes = _rank_root_causes(member_events, registry)
        affected, not_on_chain = _impact_surface(services, ranking, registry)
        impact_summary = _build_summary(
            fault_seq, services, ranking, affected, is_closed, open_triggers, release_notes
        )
        faults.append(
            FaultCluster(
                fault_id=f"F-{fault_seq:03d}",
                candidate_ids=candidate_ids,
                event_ids=[event.event_id for event in member_events],
                corroborating_ids=[],
                services=services,
                start_ts=min(event.ts_ms for event in member_events),
                end_ts=end_ts,
                is_closed=is_closed,
                open_triggers=open_triggers,
                root_cause_ranking=ranking,
                affected_downstream=affected,
                not_on_dependency_chain=not_on_chain,
                impact_summary=impact_summary,
                boundary_basis=boundary_basis,
            )
        )

    faults.sort(key=lambda cluster: (cluster.start_ts, cluster.fault_id))
    for index, cluster in enumerate(faults, start=1):
        cluster.fault_id = f"F-{index:03d}"

    unassigned = _attach_corroborating(
        events, faults, context_clusters, event_index, corroborating_tolerance_ms
    )
    return FaultBuildResult(faults, context_clusters, unassigned)


def _resolve_boundary(member_events: list[Event]):
    earliest = min(event.ts_ms for event in member_events)
    latest = max(event.ts_ms for event in member_events)
    basis = [
        {"basis": "initial_min", "ts_ms": earliest, "reason": "簇内最早事件逻辑时刻作为开始时刻初值"},
        {"basis": "initial_max", "ts_ms": latest, "reason": "簇内最晚事件逻辑时刻作为结束时刻初值"},
    ]
    triggers = [
        event
        for event in member_events
        if event.source in ("alert", "probe") and event.status == "trigger"
    ]
    recoveries = [
        event
        for event in member_events
        if event.source in ("alert", "probe") and event.status == "recovery"
    ]
    recovery_codes = {(event.service, event.code) for event in recoveries}
    open_triggers = sorted(
        {
            f"{event.service}:{event.code}"
            for event in triggers
            if (event.service, event.code) not in recovery_codes
        }
    )
    is_closed = not open_triggers
    if recoveries:
        recovery_end = max(event.ts_ms for event in recoveries)
        end_ts = max(latest, recovery_end)
        basis.append(
            {
                "basis": "recovery_corrected",
                "ts_ms": recovery_end,
                "reason": "结合告警/探测恢复事件修正结束时刻",
            }
        )
    else:
        end_ts = latest
        basis.append(
            {
                "basis": "observed_max_without_recovery",
                "ts_ms": latest,
                "reason": "无任何恢复事件，结束时刻仅取簇内观测到的最晚事件，绝不伪造为观测窗口末端",
            }
        )
    if not is_closed:
        basis.append(
            {
                "basis": "open_loop",
                "ts_ms": end_ts,
                "reason": f"以下触发缺少同服务同事件码恢复: {', '.join(open_triggers)}",
            }
        )
    return end_ts, is_closed, open_triggers, basis


def _rank_root_causes(member_events: list[Event], registry: DependencyRegistry):
    anomaly = [
        event
        for event in member_events
        if event.source in ("alert", "probe") and event.status == "trigger"
    ]
    anomaly.sort(key=lambda item: (item.ts_ms, item.event_id))
    first_seen: dict[str, int] = {}
    anomaly_count: dict[str, int] = {}
    for event in anomaly:
        first_seen.setdefault(event.service, event.ts_ms)
        anomaly_count[event.service] = anomaly_count.get(event.service, 0) + 1

    releases = [event for event in member_events if event.source == "release"]
    released_services = {event.service for event in releases}
    earliest_anomaly_ts = min(first_seen.values())

    ranking = []
    for order, service in enumerate(sorted(first_seen, key=lambda item: (first_seen[item], item)), start=1):
        downstream = registry.downstream(service) & set(first_seen)
        upstream = registry.upstream(service) & set(first_seen)
        reasons = [
            f"最早异常逻辑时刻 {first_seen[service]}（相对簇内最早异常偏移 {first_seen[service] - earliest_anomaly_ts}ms）",
            f"簇内异常触发 {anomaly_count[service]} 条",
        ]
        if downstream:
            reasons.append(f"异常下游服务: {sorted(downstream)}")
        if upstream:
            reasons.append(f"其自身也有上游异常源: {sorted(upstream)}")
        if service in released_services:
            reasons.append("故障窗口内存在该服务的发布/回滚事件，部署相关性高")
        ranking.append(
            {
                "rank": order,
                "service": service,
                "first_anomaly_ts": first_seen[service],
                "anomaly_count": anomaly_count[service],
                "has_release_in_window": service in released_services,
                "reasons": reasons,
            }
        )
    release_notes = []
    for event in sorted(releases, key=lambda item: (item.ts_ms, item.event_id)):
        release_notes.append(
            {
                "event_id": event.event_id,
                "service": event.service,
                "code": event.code,
                "ts_ms": event.ts_ms,
                "status": event.status,
            }
        )
    return ranking, release_notes


def _impact_surface(services: list[str], ranking: list[dict], registry: DependencyRegistry):
    root_services = [item["service"] for item in ranking]
    top_root = root_services[0]
    reachable = registry.downstream(top_root)
    affected = sorted(set(services).intersection(reachable))
    not_on_chain = sorted(set(services) - {top_root} - set(affected))
    return affected, not_on_chain


def _build_summary(fault_seq, services, ranking, affected, is_closed, open_triggers, release_notes):
    root_text = ", ".join(
        f"{item['rank']}.{item['service']}@{item['first_anomaly_ts']}" for item in ranking
    )
    state = "已闭环" if is_closed else f"未闭环（缺少恢复: {', '.join(open_triggers)}）"
    release_text = f"；窗口内发布/回滚 {len(release_notes)} 次" if release_notes else ""
    return (
        f"故障 F-{fault_seq:03d}：涉及服务 {services}；根因候选排序 {root_text}；"
        f"依赖链上受影响下游 {affected if affected else '无'}；状态 {state}{release_text}"
    )


def _attach_corroborating(events, faults, context_clusters, event_index, tolerance_ms=0):
    corroborating = [
        event for event in events if event.source in CORROBORATING_SOURCES
    ]
    unassigned: list[dict] = []
    for event in sorted(corroborating, key=lambda item: (item.ts_ms, item.event_id)):
        target = None
        for cluster in faults:
            upper = cluster.end_ts if cluster.end_ts is not None else cluster.start_ts
            if (
                event.service in cluster.services
                and cluster.start_ts <= event.ts_ms <= upper + tolerance_ms
            ):
                target = cluster
                break
        if target is None:
            for context in context_clusters:
                if (
                    event.service in context["services"]
                    and context["start_ts"] <= event.ts_ms <= context["end_ts"] + tolerance_ms
                ):
                    context.setdefault("corroborating_ids", []).append(event.event_id)
                    target = context
                    break
        if target is None:
            unassigned.append(
                {
                    "event_id": event.event_id,
                    "service": event.service,
                    "ts_ms": event.ts_ms,
                    "reason": "逻辑时刻不落在任何簇范围内，保留为未挂载佐证事件",
                }
            )
            continue
        if isinstance(target, FaultCluster):
            target.corroborating_ids.append(event.event_id)
    for cluster in faults:
        cluster.corroborating_ids.sort()
    return unassigned
