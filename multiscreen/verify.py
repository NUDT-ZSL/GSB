from __future__ import annotations

from typing import Any

from .canon import canonical
from .engine import new_receiver_state
from .errors import E_REPLAY
from .replay import run_replay

COMPARE_FIELDS = (
    "adoption_cursor_seq",
    "playback_position_ms",
    "generation",
    "wait_gap",
    "gap_intervals",
)


def gap_signature(gap: dict[str, Any] | None) -> dict[str, Any] | None:
    if not gap:
        return None
    return {
        "status": gap.get("status", "open"),
        "start_ms": gap.get("start_ms"),
        "end_ms": gap.get("end_ms"),
        "end_reason": gap.get("end_reason"),
        "after_cursor_seq": gap.get("after_cursor_seq"),
        "expected_frame_seq": gap.get("expected_frame_seq"),
        "observed_frame_seq": gap.get("observed_frame_seq"),
        "missing_frame_seqs": sorted(gap.get("missing_frame_seqs", []) or []),
        "rejected_frame_seqs": sorted(gap.get("rejected_frame_seqs", []) or []),
        "discarded_frame_seqs": sorted(gap.get("discarded_frame_seqs", []) or []),
    }


def build_manifest_initial_states(manifest: dict[str, Any]) -> dict[str, dict[str, Any]]:
    states = {}
    for session in manifest.get("sessions", []):
        for receiver in session.get("receivers", []):
            rid = receiver["receiver_id"]
            tolerance = receiver.get("tolerance_ms") or session.get("switch_tolerance_ms", 500)
            states[rid] = new_receiver_state(
                rid,
                session["session_id"],
                generation=1,
                tolerance_ms=int(tolerance),
                start_ms=int(session["start_ms"]),
                generation_tolerances={1: int(tolerance), 2: int(tolerance)},
                breach_target_generation=session.get("breach_switch_generation"),
            )
    return states


def _expected_value(view_record: dict[str, Any], field: str) -> Any:
    if field == "wait_gap":
        return gap_signature(view_record.get("wait_gap"))
    if field == "gap_intervals":
        intervals = view_record.get("gap_intervals") or []
        return [gap_signature(item) for item in intervals if isinstance(item, dict)]
    return view_record.get(field)


def _actual_value(state: dict[str, Any], field: str) -> Any:
    if field == "wait_gap":
        return gap_signature(state.get("open_gap"))
    if field == "gap_intervals":
        return [gap_signature(item) for item in state.get("gap_intervals", []) if isinstance(item, dict)]
    return state.get(field)


def _same_value(left: Any, right: Any) -> bool:
    return canonical(left) == canonical(right)


def locate_first_divergence(
    trace_for_receiver: list[dict[str, Any]], field: str, expected: Any
) -> dict[str, Any] | None:
    """找到最后一次匹配之后的首个持续不一致逻辑时刻。"""

    def value(entry: dict[str, Any]) -> Any:
        state = entry["state"]
        if field == "wait_gap":
            return gap_signature(state.get("open_gap"))
        if field == "gap_intervals":
            return [gap_signature(item) for item in state.get("gap_intervals", []) if isinstance(item, dict)]
        return state.get(field)

    last_match = -1
    for index, entry in enumerate(trace_for_receiver):
        if _same_value(value(entry), expected):
            last_match = index
    candidate_index = last_match + 1
    if candidate_index >= len(trace_for_receiver):
        return None
    entry = trace_for_receiver[candidate_index]
    return {
        "logical_ms": entry["sample_ms"],
        "event_seq": entry["event_seq"],
        "receiver_id": entry["receiver_id"],
        "field": field,
        "expected": expected,
        "actual": value(entry),
        "note": "first_logical_moment_after_last_matching_state",
    }


def check_source_continuity(record: dict[str, Any], replay: dict[str, Any]) -> dict[str, Any]:
    rid = record["canonical_receiver_id"]
    trace = [item for item in replay["trace"] if item["receiver_id"] == rid]
    cursor = record.get("adoption_cursor_seq")
    generation = record.get("generation")
    ts_ms = record.get("logical_ts_ms")
    matches = []
    for entry in trace:
        state = entry["state"]
        if state.get("adoption_cursor_seq") != cursor:
            continue
        if generation is not None and state.get("generation") != generation:
            continue
        if ts_ms is not None and state.get("playback_position_ms") != ts_ms:
            continue
        matches.append(entry)
    if matches:
        entry = matches[-1]
        return {
            "receiver_id": rid,
            "source_location": record["source_location"],
            "ok": True,
            "matched_event_seq": entry["event_seq"],
            "logical_ms": entry["sample_ms"],
        }
    return {
        "receiver_id": rid,
        "source_location": record["source_location"],
        "ok": False,
        "expected": {
            "generation": generation,
            "adoption_cursor_seq": cursor,
            "playback_position_ms": ts_ms,
        },
        "code": E_REPLAY,
    }


def verify_replay(
    view: dict[str, dict[str, Any]],
    events: list[dict[str, Any]],
    manifest: dict[str, Any] | None = None,
    records: list[dict[str, Any]] | None = None,
    *,
    workers: int = 1,
    budget_bytes: int = 64 * 1024 * 1024,
    auto_switch_on_breach: bool = True,
) -> dict[str, Any]:
    initial_states = build_manifest_initial_states(manifest) if manifest else None
    replay = run_replay(
        events,
        initial_states,
        workers=workers,
        budget_bytes=budget_bytes,
        auto_switch_on_breach=auto_switch_on_breach,
    )

    divergences: list[dict[str, Any]] = []
    checks: list[dict[str, Any]] = []
    compared = 0
    tombstoned = 0

    for receiver_id in sorted(view):
        record = view[receiver_id]
        if record.get("tombstone"):
            tombstoned += 1
            checks.append(
                {
                    "receiver_id": receiver_id,
                    "ok": True,
                    "skipped": "tombstone",
                    "source_location": record.get("source_location"),
                }
            )
            continue
        compared += 1
        result = replay["results"].get(receiver_id)
        if result is None:
            divergence = {
                "logical_ms": None,
                "event_seq": None,
                "receiver_id": receiver_id,
                "field": "existence",
                "expected": "receiver_replay_state",
                "actual": None,
            }
            divergences.append(divergence)
            continue
        actual_state = result["state"]
        trace = result["trace"]
        receiver_ok = True
        for field in COMPARE_FIELDS:
            if field == "gap_intervals" and record.get("gap_intervals") is None:
                continue
            expected = _expected_value(record, field)
            actual = _actual_value(actual_state, field)
            matches = _same_value(expected, actual)
            checks.append(
                {
                    "receiver_id": receiver_id,
                    "field": field,
                    "ok": matches,
                    "expected": expected,
                    "actual": actual,
                    "source_location": record.get("source_location"),
                }
            )
            if not matches:
                receiver_ok = False
                divergence = locate_first_divergence(trace, field, expected)
                if divergence is None:
                    divergence = {
                        "logical_ms": actual_state.get("last_sample_ms"),
                        "event_seq": actual_state.get("last_event_seq"),
                        "receiver_id": receiver_id,
                        "field": field,
                        "expected": expected,
                        "actual": actual,
                        "note": "final_state_mismatch",
                    }
                divergences.append(divergence)
        if receiver_ok:
            checks.append({"receiver_id": receiver_id, "ok": True, "matched_fields": list(COMPARE_FIELDS)})

    source_checks = [check_source_continuity(record, replay) for record in (records or [])]
    source_failures = [item for item in source_checks if not item["ok"]]
    for failure in source_failures:
        divergences.append(
            {
                "logical_ms": None,
                "event_seq": None,
                "receiver_id": failure["receiver_id"],
                "field": "source_continuity",
                "expected": failure["expected"],
                "actual": failure["source_location"],
            }
        )

    divergences.sort(
        key=lambda item: (
            item.get("logical_ms") if isinstance(item.get("logical_ms"), int) else 1 << 62,
            item.get("receiver_id", ""),
            item.get("field", ""),
            item.get("event_seq") if isinstance(item.get("event_seq"), int) else 1 << 62,
        )
    )
    stats = {
        "events_total": len(events),
        "receivers_in_view": len(view),
        "receivers_compared": compared,
        "receivers_tombstoned": tombstoned,
        "trace_records": len(replay["trace"]),
        "rebuild_reports": len(replay["rebuilds"]),
        "counts": replay["counts"],
        "source_records_checked": len(source_checks),
        "source_records_failed": len(source_failures),
    }
    if divergences:
        return {
            "ok": False,
            "code": E_REPLAY,
            "message": "合并视图与帧事件重放结果不一致",
            "first_divergence": divergences[0],
            "divergences": divergences,
            "checks": checks,
            "source_continuity_checks": source_checks,
            "stats": stats,
            "summary": f"E_REPLAY: 发现 {len(divergences)} 项偏差，首个偏差位于 "
            f"{divergences[0].get('receiver_id')}@{divergences[0].get('logical_ms')}ms/"
            f"{divergences[0].get('field')}",
            "replay": replay,
        }
    return {
        "ok": True,
        "code": None,
        "message": "合并视图与帧事件重放结果一致",
        "first_divergence": None,
        "divergences": [],
        "checks": checks,
        "source_continuity_checks": source_checks,
        "stats": stats,
        "summary": f"校验通过：{compared} 个有效接收端、{len(events)} 条事件、"
        f"{len(replay['rebuilds'])} 次代数重建均一致",
        "replay": replay,
    }
