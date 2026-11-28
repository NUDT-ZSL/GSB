"""汇总流水线：全量与增量共用同一套确定性计算，保证结果可重复。"""
from __future__ import annotations

from .accounting import build_accounts
from .clustering import build_candidates, merge_candidates
from .dedup import deduplicate
from .faults import build_faults
from .normalize import load_events
from .registry import DependencyRegistry
from .storage import latest_run_id, load_snapshot
from .timeline import build_timeline


DEFAULT_PARAMS = {
    "storm_window_ms": 60_000,
    "proximity_window_ms": 120_000,
    "merge_window_ms": 120_000,
}


def run_full(
    event_file: str,
    registry: DependencyRegistry,
    params: dict,
    batch_id: str,
) -> dict:
    events = load_events(event_file, registry, batch_id)
    return _assemble(events, registry, params, event_file, batch_id, "full", None)


def run_incremental(
    event_file: str,
    registry: DependencyRegistry,
    params: dict,
    batch_id: str,
    connection,
) -> dict:
    previous_run_id = latest_run_id(connection)
    if previous_run_id is None:
        raise RuntimeError("增量重算要求 SQLite 中已存在一次全量运行结果")
    previous = load_snapshot(connection, previous_run_id)
    events = load_events(event_file, registry, batch_id)
    result = _assemble(events, registry, params, event_file, batch_id, "incremental", previous)
    result["previous_run_id"] = previous_run_id
    return result


def _assemble(events, registry, params, event_file, batch_id, mode, previous):
    kept, removed, dedup_records = deduplicate(events, params["storm_window_ms"])
    candidates = build_candidates(kept, params["proximity_window_ms"])
    groups, merge_records = merge_candidates(candidates, registry, params["merge_window_ms"])
    build_result = build_faults(groups, kept, registry, params["proximity_window_ms"])
    faults = build_result.faults

    event_index = {event.event_id: event for event in kept}
    accounts = build_accounts(
        events,
        kept,
        removed,
        dedup_records,
        faults,
        build_result.context_clusters,
        build_result.unassigned_corroborating,
    )
    timeline = build_timeline(faults, event_index)

    incremental_changes = []
    if mode == "incremental" and previous is not None:
        incremental_changes = _diff_with_previous(faults, timeline, accounts, previous)

    summary = _build_summary(faults, build_result, accounts, timeline, incremental_changes)
    return {
        "mode": mode,
        "event_file": event_file,
        "batch_id": batch_id,
        "params": params,
        "events": events,
        "kept_events": kept,
        "removed_events": removed,
        "dedup_records": dedup_records,
        "candidates": candidates,
        "merge_records": merge_records,
        "faults": faults,
        "context_clusters": build_result.context_clusters,
        "unassigned_corroborating": build_result.unassigned_corroborating,
        "accounts": accounts,
        "timeline": timeline,
        "incremental_changes": incremental_changes,
        "summary": summary,
    }


def _diff_with_previous(faults, timeline, accounts, previous):
    previous_faults = {item["fault_id"]: item for item in previous["faults"]}
    previous_event_owner: dict[str, str] = {}
    previous_roots: dict[str, dict] = {}
    for fault in previous["faults"]:
        previous_roots[fault["fault_id"]] = fault
        for event_id in fault["event_ids"]:
            previous_event_owner[event_id] = fault["fault_id"]

    current_owner: dict[str, str] = {}
    for fault in faults:
        for event_id in fault.event_ids:
            current_owner[event_id] = fault.fault_id

    changes = []
    identity_map: dict[str, str] = {}
    used_previous: set[str] = set()
    for fault in faults:
        prior_id = _match_prior_fault(fault, previous_event_owner)
        if prior_id is not None:
            identity_map[fault.fault_id] = prior_id
            used_previous.add(prior_id)
            prior = previous_roots[prior_id]
            detail = _boundary_detail(fault, prior)
            if detail["changed"]:
                fault.changed_vs_previous = "boundary_changed"
                changes.append(
                    {
                        "fault_id": fault.fault_id,
                        "previous_fault_id": prior_id,
                        "change_type": "boundary_changed",
                        "detail": detail,
                    }
                )
            else:
                fault.changed_vs_previous = "unchanged"
        else:
            fault.changed_vs_previous = "new"
            changes.append(
                {
                    "fault_id": fault.fault_id,
                    "previous_fault_id": None,
                    "change_type": "new",
                    "detail": {"reason": "新批次事件形成的此前不存在的故障簇"},
                }
            )

    for prior_id, prior in previous_roots.items():
        if prior_id not in used_previous:
            changes.append(
                {
                    "fault_id": None,
                    "previous_fault_id": prior_id,
                    "change_type": "disappeared",
                    "detail": {"reason": "重算后未再匹配到该故障簇（事件被并入其他簇或不再构成故障）"},
                }
            )

    untouched = _verify_untouched(faults, identity_map, previous, timeline, accounts)
    for item in untouched:
        changes.append(
            {
                "fault_id": item["fault_id"],
                "previous_fault_id": item["previous_fault_id"],
                "change_type": "verified_unchanged",
                "detail": item["detail"],
            }
        )
    return changes


def _match_prior_fault(fault, previous_event_owner):
    votes: dict[str, int] = {}
    for event_id in fault.event_ids:
        prior_id = previous_event_owner.get(event_id)
        if prior_id:
            votes[prior_id] = votes.get(prior_id, 0) + 1
    if not votes:
        return None
    return sorted(votes.items(), key=lambda item: (-item[1], item[0]))[0][0]


def _boundary_detail(fault, prior):
    start_changed = fault.start_ts != prior["start_ts"]
    end_changed = fault.end_ts != prior["end_ts"]
    closed_changed = fault.is_closed != prior["is_closed"]
    events_added = sorted(set(fault.event_ids) - set(prior["event_ids"]))
    events_removed = sorted(set(prior["event_ids"]) - set(fault.event_ids))
    return {
        "changed": start_changed or end_changed or closed_changed or bool(events_added) or bool(events_removed),
        "start_ts": {"previous": prior["start_ts"], "current": fault.start_ts, "changed": start_changed},
        "end_ts": {"previous": prior["end_ts"], "current": fault.end_ts, "changed": end_changed},
        "is_closed": {"previous": prior["is_closed"], "current": fault.is_closed, "changed": closed_changed},
        "events_added": events_added,
        "events_removed": events_removed,
    }


def _verify_untouched(faults, identity_map, previous, timeline, accounts):
    previous_entries = {entry["fault_id"]: entry for entry in previous["timeline"]}
    current_entries = {entry["fault_id"]: entry for entry in timeline["entries"]}
    verified = []
    for fault in faults:
        prior_id = identity_map.get(fault.fault_id)
        if prior_id is None or fault.changed_vs_previous != "unchanged":
            continue
        prior_entry = previous_entries.get(prior_id)
        current_entry = current_entries.get(fault.fault_id)
        comparable_prior = _strip_volatile(dict(prior_entry)) if prior_entry else None
        comparable_current = _strip_volatile(dict(current_entry)) if current_entry else None
        timeline_equal = comparable_prior == comparable_current
        verified.append(
            {
                "fault_id": fault.fault_id,
                "previous_fault_id": prior_id,
                "detail": {
                    "timeline_fieldwise_equal": timeline_equal,
                    "accounting_dimensions_preserved": _accounting_preserved(accounts, previous["accounts"]),
                    "reason": "未受新批次影响：时间线条目逐字段一致，账目维度行保留",
                },
            }
        )
    return verified


def _strip_volatile(entry):
    entry.pop("changed_vs_previous", None)
    entry.pop("order", None)
    return entry


def _accounting_preserved(current_accounts, previous_accounts):
    current_source = {row["key"]: row for row in current_accounts["by_source"]}
    previous_source = {row["key"]: row for row in previous_accounts["by_source"]}
    for key, previous_row in previous_source.items():
        current_row = current_source.get(key)
        if current_row is None:
            return False
        if current_row["deduped_count"] < previous_row["deduped_count"]:
            return False
    return True


def _build_summary(faults, build_result, accounts, timeline, changes):
    open_faults = [fault.fault_id for fault in faults if not fault.is_closed]
    return {
        "fault_count": len(faults),
        "closed_fault_count": sum(1 for fault in faults if fault.is_closed),
        "open_fault_ids": open_faults,
        "context_cluster_count": len(build_result.context_clusters),
        "unassigned_corroborating_count": len(build_result.unassigned_corroborating),
        "total_input": accounts["total_input"],
        "dedup_removed_total": accounts["reconciliation"]["dedup_removed_total"],
        "accounting_all_ok": accounts["reconciliation"]["all_ok"],
        "timeline_consistent": timeline["consistent"],
        "incremental_change_count": len(changes),
    }
