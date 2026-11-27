"""Workload orchestration and independent-reference verification."""

from __future__ import annotations

from typing import Any

from .engine import IndexEngine
from .loader import normalized_timeline, validate_workload
from .model import IndexConfig, QualityEvent
from .reference import ReferenceIndex


def _sequence_mismatch(
    engine_hits: list[dict[str, Any]],
    reference_records: list[dict[str, Any]],
) -> dict[str, Any] | None:
    engine_sequence = [
        (str(hit["event_id"]), tuple(hit["key"])) for hit in engine_hits
    ]
    reference_sequence = [
        (str(record["event_id"]), tuple(record["_key"]))
        for record in reference_records
    ]
    if engine_sequence == reference_sequence:
        return None
    limit = max(len(engine_sequence), len(reference_sequence))
    for index in range(limit):
        engine_item = (
            engine_sequence[index] if index < len(engine_sequence) else None
        )
        reference_item = (
            reference_sequence[index]
            if index < len(reference_sequence)
            else None
        )
        if engine_item != reference_item:
            return {
                "rank": index + 1,
                "engine": (
                    {
                        "event_id": engine_item[0],
                        "key": list(engine_item[1]),
                    }
                    if engine_item is not None
                    else None
                ),
                "reference": (
                    {
                        "event_id": reference_item[0],
                        "key": list(reference_item[1]),
                    }
                    if reference_item is not None
                    else None
                ),
                "reason": "命中事件或有序键首次不一致",
            }
    return {
        "rank": None,
        "engine": None,
        "reference": None,
        "reason": "未定位到差异",
    }


def _verify_state(
    engine: IndexEngine, reference: ReferenceIndex
) -> dict[str, Any]:
    checked_segments = 0
    checked_memtable_entries = 0
    first_segment_mismatch: dict[str, Any] | None = None
    seen_event_ids: dict[str, str] = {}

    def record_segment_issue(
        segment_id: str,
        offset: int | None,
        event_id: str | None,
        engine_key: list[Any] | None,
        reference_key: list[Any] | None,
        reason: str,
    ) -> None:
        nonlocal first_segment_mismatch
        if first_segment_mismatch is None:
            first_segment_mismatch = {
                "segment_id": segment_id,
                "segment_offset": offset,
                "event_id": event_id,
                "engine_key": engine_key,
                "reference_key": reference_key,
                "reason": reason,
            }

    for segment_id in sorted(engine.live_segments):
        segment = engine.live_segments[segment_id]
        checked_segments += 1
        keys = [entry.key for entry in segment.entries]
        if keys != sorted(keys):
            for offset, entry in enumerate(segment.entries):
                expected = sorted(keys)[offset]
                if entry.key != expected:
                    record_segment_issue(
                        segment_id,
                        offset,
                        entry.event_id,
                        list(entry.key),
                        list(expected),
                        "段内条目未按键升序",
                    )
                    break
        if len(keys) != len(set(keys)):
            record_segment_issue(
                segment_id,
                None,
                None,
                None,
                None,
                "段内存在重复复合键",
            )
        mapping = segment.key_event_id_mapping()
        if len(mapping) != len(segment.entries):
            record_segment_issue(
                segment_id,
                None,
                None,
                None,
                None,
                "键到业务事件标识映射不唯一",
            )
        for offset, entry in enumerate(segment.entries):
            if entry.event_id in seen_event_ids:
                record_segment_issue(
                    segment_id,
                    offset,
                    entry.event_id,
                    list(entry.key),
                    None,
                    (
                        "同一业务事件出现在多个存活位置: "
                        f"{seen_event_ids[entry.event_id]}"
                    ),
                )
            else:
                seen_event_ids[entry.event_id] = segment_id
            reference_record = reference.records.get(entry.event_id)
            reference_key = (
                list(reference_record["_key"])
                if reference_record is not None
                else None
            )
            if reference_record is None:
                record_segment_issue(
                    segment_id,
                    offset,
                    entry.event_id,
                    list(entry.key),
                    None,
                    "段条目在独立写入记录中不存在",
                )
            elif tuple(reference_key) != entry.key:
                record_segment_issue(
                    segment_id,
                    offset,
                    entry.event_id,
                    list(entry.key),
                    reference_key,
                    "段条目键与独立参考键不一致",
                )

    for partition_key in sorted(engine.partitions):
        partition = engine.partitions[partition_key]
        if partition.memtable_id is None:
            continue
        memtable_keys = [
            entry.key for entry in partition.memtable_entries
        ]
        if memtable_keys != sorted(memtable_keys):
            record_segment_issue(
                f"MEMTABLE:{partition.memtable_id}",
                0,
                None,
                None,
                None,
                "内存表键序未保持升序",
            )
        for entry in partition.memtable_entries:
            checked_memtable_entries += 1
            if entry.event_id in seen_event_ids:
                record_segment_issue(
                    f"MEMTABLE:{partition.memtable_id}",
                    entry.logical_offset,
                    entry.event_id,
                    list(entry.key),
                    list(reference.records[entry.event_id]["_key"])
                    if entry.event_id in reference.records
                    else None,
                    (
                        "同一业务事件同时出现在段和内存表: "
                        f"{seen_event_ids[entry.event_id]}"
                    ),
                )
            else:
                seen_event_ids[entry.event_id] = (
                    f"MEMTABLE:{partition.memtable_id}"
                )
            reference_record = reference.records.get(entry.event_id)
            if reference_record is None:
                record_segment_issue(
                    f"MEMTABLE:{partition.memtable_id}",
                    entry.logical_offset,
                    entry.event_id,
                    list(entry.key),
                    None,
                    "内存表条目在独立写入记录中不存在",
                )
            elif tuple(reference_record["_key"]) != entry.key:
                record_segment_issue(
                    f"MEMTABLE:{partition.memtable_id}",
                    entry.logical_offset,
                    entry.event_id,
                    list(entry.key),
                    list(reference_record["_key"]),
                    "内存表条目键与独立参考键不一致",
                )

    engine_invalid = set(engine.invalid_event_ids)
    reference_invalid = set(reference.invalid_event_ids)
    invalid_match = engine_invalid == reference_invalid
    invalid_difference = {
        "engine_only": sorted(engine_invalid - reference_invalid),
        "reference_only": sorted(reference_invalid - engine_invalid),
    }

    engine_active_ids = {
        entry.event_id
        for entry, _ in engine._all_entries_with_source()
        if entry.event_id not in engine.invalid_event_ids
    }
    reference_active_ids = {
        record["event_id"] for record in reference.active_records()
    }
    active_match = engine_active_ids == reference_active_ids
    active_difference = {
        "engine_only": sorted(engine_active_ids - reference_active_ids),
        "reference_only": sorted(reference_active_ids - engine_active_ids),
    }

    first_active_difference: dict[str, Any] | None = None
    if not active_match:
        event_id = sorted(
            engine_active_ids.symmetric_difference(reference_active_ids)
        )[0]
        reference_record = reference.records.get(event_id)
        engine_position = next(
            (
                (entry.key, source)
                for entry, source in engine._all_entries_with_source()
                if entry.event_id == event_id
            ),
            None,
        )
        first_active_difference = {
            "event_id": event_id,
            "engine_key": (
                list(engine_position[0]) if engine_position is not None else None
            ),
            "reference_key": (
                list(reference_record["_key"])
                if reference_record is not None
                else None
            ),
        }

    match = (
        invalid_match
        and active_match
        and first_segment_mismatch is None
    )
    return {
        "match": match,
        "checked_segments": checked_segments,
        "checked_memtable_entries": checked_memtable_entries,
        "active_entry_count": len(engine_active_ids),
        "invalid_entry_count": len(engine_invalid),
        "invalid_marker_match": invalid_match,
        "invalid_marker_difference": invalid_difference,
        "active_set_match": active_match,
        "active_set_difference": active_difference,
        "first_active_difference": first_active_difference,
        "first_segment_mismatch": first_segment_mismatch,
    }


def solve_workload(workload: dict[str, Any]) -> dict[str, Any]:
    validate_workload(workload)
    config = IndexConfig.from_dict(workload["config"])
    engine = IndexEngine(config)
    reference = ReferenceIndex()
    steps: list[dict[str, Any]] = []
    reclamation_actions: list[dict[str, Any]] = []
    query_results: list[dict[str, Any]] = []
    state_checks: list[dict[str, Any]] = []
    invalidation_checks: list[dict[str, Any]] = []
    first_query_mismatch: dict[str, Any] | None = None
    first_segment_mismatch: dict[str, Any] | None = None
    first_invalidation_mismatch: dict[str, Any] | None = None
    all_accounting_ok = True

    for step in normalized_timeline(workload):
        time = int(step["time"])
        writes = step["writes"]
        events = [QualityEvent.from_dict(raw) for raw in writes]
        actions = engine.ingest_time_group(time, events)
        reference.ingest(writes)

        invalidation_reports: list[dict[str, Any]] = []
        invalidation_actions: list[dict[str, Any]] = []
        for spec in step["invalidations"]:
            invalidation_action = engine.invalidate(spec, time)
            reference_new = reference.invalidate(spec)
            engine_new = sorted(
                invalidation_action["newly_invalidated_event_ids"]
            )
            reference_new_sorted = sorted(reference_new)
            stale_hit = invalidation_action["diagnostic"][
                "first_inconsistency"
            ]
            check_ok = (
                engine_new == reference_new_sorted and stale_hit is None
            )
            invalidation_action["reference_check"] = {
                "match": check_ok,
                "engine_newly_invalidated_event_ids": engine_new,
                "reference_newly_invalidated_event_ids": reference_new_sorted,
                "first_difference": next(
                    (
                        {
                            "engine_event_id": (
                                engine_new[index]
                                if index < len(engine_new)
                                else None
                            ),
                            "reference_event_id": (
                                reference_new_sorted[index]
                                if index < len(reference_new_sorted)
                                else None
                            ),
                        }
                        for index in range(
                            max(len(engine_new), len(reference_new_sorted))
                        )
                        if (
                            index >= len(engine_new)
                            or index >= len(reference_new_sorted)
                            or engine_new[index]
                            != reference_new_sorted[index]
                        )
                    ),
                    None,
                ),
            }
            invalidation_check = {
                "time": time,
                "invalidation_id": spec.get("invalidation_id"),
                "match": check_ok,
                "engine_new_count": len(engine_new),
                "reference_new_count": len(reference_new_sorted),
                "stale_hit": stale_hit,
                "no_stale_hit": stale_hit is None,
            }
            invalidation_checks.append(invalidation_check)
            if not check_ok and first_invalidation_mismatch is None:
                first_invalidation_mismatch = invalidation_check
            invalidation_actions.append(invalidation_action)
            invalidation_reports.append(invalidation_action)

        maintenance_actions: list[dict[str, Any]] = []
        for invalidation_action in invalidation_actions:
            maintenance_actions.extend(
                engine.maintain_after_invalidation(
                    time, invalidation_action
                )
            )
        actions.extend(invalidation_actions)
        actions.extend(maintenance_actions)

        for action in actions:
            if action["action"] != "compact":
                continue
            reclamation_actions.append(
                {
                    "sequence": action["sequence"],
                    "time": action["time"],
                    "partition": action["partition"],
                    "trigger": action["trigger"],
                    "source_segment_ids": action["source_segment_ids"],
                    "target_segment_id": (
                        action["target_segment"]["segment_id"]
                        if action["target_segment"] is not None
                        else None
                    ),
                    "discarded_invalid_entry_count": action[
                        "discarded_invalid_entry_count"
                    ],
                    "partition_bytes_before": action[
                        "partition_bytes_before"
                    ],
                    "partition_bytes_after": action[
                        "partition_bytes_after"
                    ],
                    "deleted_segment_bytes": action[
                        "deleted_segment_bytes"
                    ],
                    "rewritten_segment_bytes": action[
                        "rewritten_segment_bytes"
                    ],
                    "reclaimed_bytes": action["reclaimed_bytes"],
                    "ledger_ok": action["ledger"]["ok"],
                }
            )

        state_check = _verify_state(engine, reference)
        state_check["time"] = time
        state_checks.append(state_check)
        if not state_check["match"] and first_segment_mismatch is None:
            first_segment_mismatch = {
                "time": time,
                **state_check,
            }

        step_queries: list[dict[str, Any]] = []
        for query in step["queries"]:
            query_id = str(query["query_id"])
            engine_result = engine.query(
                query_id,
                str(query["process"]),
                str(query["shift"]),
                str(query["batch_prefix"]),
            )
            reference_hits = reference.query(
                str(query["process"]),
                str(query["shift"]),
                str(query["batch_prefix"]),
            )
            mismatch = _sequence_mismatch(engine_result["hits"], reference_hits)
            verification = {
                "match": mismatch is None,
                "reference_hit_count": len(reference_hits),
                "reference_event_ids": [
                    record["event_id"] for record in reference_hits
                ],
                "first_mismatch": mismatch,
            }
            engine_result["verification"] = verification
            step_queries.append(engine_result)
            query_results.append(
                {
                    "time": time,
                    "query_id": query_id,
                    "match": mismatch is None,
                    "mismatch": mismatch,
                }
            )
            if mismatch is not None and first_query_mismatch is None:
                first_query_mismatch = {
                    "time": time,
                    "query_id": query_id,
                    "process": str(query["process"]),
                    "shift": str(query["shift"]),
                    "batch_prefix": str(query["batch_prefix"]),
                    "first_mismatch": mismatch,
                }

        partition_views = engine.partition_views()
        step_accounting_ok = all(
            view["accounting_ok"] for view in partition_views
        )
        all_accounting_ok = all_accounting_ok and step_accounting_ok
        steps.append(
            {
                "time": time,
                "write_count": len(writes),
                "write_event_ids": [
                    str(event["event_id"]) for event in writes
                ],
                "actions": sorted(
                    actions, key=lambda action: action["sequence"]
                ),
                "invalidations": invalidation_reports,
                "queries": step_queries,
                "partition_views": partition_views,
                "accounting_ok": step_accounting_ok,
                "state_verification": state_check,
            }
        )

    actions_flat = [
        action for step in steps for action in step["actions"]
    ]
    action_counts: dict[str, int] = {}
    for action in actions_flat:
        action_counts[action["action"]] = (
            action_counts.get(action["action"], 0) + 1
        )
    all_queries_match = first_query_mismatch is None and all(
        result["match"] for result in query_results
    )
    all_state_match = all(check["match"] for check in state_checks)
    all_invalidations_match = first_invalidation_mismatch is None and all(
        check["match"] for check in invalidation_checks
    )
    reclaimed_total = sum(
        action["reclaimed_bytes"] for action in reclamation_actions
    )
    deleted_segment_count = sum(
        len(action["source_segment_ids"])
        for action in reclamation_actions
    )
    final_views = engine.partition_views()
    final_accounting_ok = all(
        view["accounting_ok"] for view in final_views
    )
    all_ok = (
        all_queries_match
        and all_state_match
        and all_invalidations_match
        and all_accounting_ok
        and final_accounting_ok
    )
    return {
        "format": "quality-index-report/v1",
        "seed": workload.get("seed"),
        "policy": IndexEngine.POLICY,
        "config": config.to_dict(),
        "steps": steps,
        "reclamation_actions": reclamation_actions,
        "final_partition_views": final_views,
        "final_segment_manifest": engine.segment_manifest(
            include_entries=True
        ),
        "summary": {
            "time_steps": len(steps),
            "write_count": len(engine.records),
            "query_count": len(query_results),
            "action_counts": action_counts,
            "segment_count_created": engine.next_segment_seq - 1,
            "segment_count_live": len(engine.live_segments),
            "segments_deleted_total": deleted_segment_count,
            "reclaimed_bytes_total": reclaimed_total,
            "all_queries_match_reference": all_queries_match,
            "all_states_match_reference": all_state_match,
            "all_invalidations_match_reference": all_invalidations_match,
            "all_partition_accounting_ok": all_accounting_ok
            and final_accounting_ok,
            "all_ok": all_ok,
        },
        "verification": {
            "reference": "qindex.reference.ReferenceIndex",
            "queries": query_results,
            "first_query_mismatch": first_query_mismatch,
            "first_segment_mismatch": first_segment_mismatch,
            "first_invalidation_mismatch": first_invalidation_mismatch,
            "all_queries_match": all_queries_match,
            "all_segments_match": all_state_match,
            "all_invalidations_match": all_invalidations_match,
            "all_accounting_ok": all_accounting_ok
            and final_accounting_ok,
            "status": "ok" if all_ok else "mismatch",
        },
    }
