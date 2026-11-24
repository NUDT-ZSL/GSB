from __future__ import annotations

from copy import deepcopy
from typing import Any

from .canon import canonical

ADOPT_KEY = "adopt_keyframe"
ADOPT_DELTA = "adopt_delta"
GAP_WAIT = "gap_wait"
DISCARD = "discard_frame"
SWITCH = "generation_switch"


def new_receiver_state(
    receiver_id: str,
    session_id: str | None = None,
    *,
    generation: int = 1,
    tolerance_ms: int = 500,
    start_ms: int = 0,
    generation_tolerances: dict[int, int] | None = None,
    breach_target_generation: int | None = None,
) -> dict[str, Any]:
    return {
        "receiver_id": receiver_id,
        "session_id": session_id,
        "generation": generation,
        "adoption_cursor_seq": 0,
        "keyframe_seq": 0,
        "keyframe_ms": None,
        "keyframe_generation": None,
        "playback_position_ms": start_ms,
        "start_ms": start_ms,
        "tolerance_ms": tolerance_ms,
        "generation_tolerances": {
            str(generation): int(value) for generation, value in (generation_tolerances or {}).items()
        },
        "status": "idle",
        "open_gap": None,
        "gap_intervals": [],
        "adopted_count": 0,
        "rejected_count": 0,
        "discarded_count": 0,
        "last_event_seq": None,
        "last_sample_ms": None,
        "pending_rebuild_id": None,
        "keyframe_adoption": None,
        "adopted_increments_since_keyframe": [],
        "breach_target_generation": breach_target_generation,
    }


def _gap_summary(gap: dict[str, Any] | None) -> dict[str, Any] | None:
    if gap is None:
        return None
    return {
        "gap_id": gap["gap_id"],
        "status": gap["status"],
        "start_event_seq": gap["start_event_seq"],
        "start_ms": gap["start_ms"],
        "end_event_seq": gap.get("end_event_seq"),
        "end_ms": gap.get("end_ms"),
        "end_reason": gap.get("end_reason"),
        "generation": gap["generation"],
        "after_cursor_seq": gap["after_cursor_seq"],
        "expected_frame_seq": gap["expected_frame_seq"],
        "observed_frame_seq": gap.get("observed_frame_seq"),
        "missing_frame_seqs": list(gap.get("missing_frame_seqs", [])),
        "rejected_frame_seqs": list(gap.get("rejected_frame_seqs", [])),
        "discarded_frame_seqs": list(gap.get("discarded_frame_seqs", [])),
    }


def public_state(state: dict[str, Any]) -> dict[str, Any]:
    """剥离仅用于过程控制的字段，保留可比较的逐接收端规范状态。"""
    return {
        "receiver_id": state["receiver_id"],
        "session_id": state.get("session_id"),
        "generation": state["generation"],
        "adoption_cursor_seq": state["adoption_cursor_seq"],
        "keyframe_seq": state["keyframe_seq"],
        "keyframe_ms": state["keyframe_ms"],
        "keyframe_generation": state.get("keyframe_generation"),
        "playback_position_ms": state["playback_position_ms"],
        "tolerance_ms": state["tolerance_ms"],
        "generation_tolerances": {
            str(generation): int(value)
            for generation, value in state.get("generation_tolerances", {}).items()
        },
        "status": state["status"],
        "open_gap": _gap_summary(state.get("open_gap")),
        "gap_intervals": [_gap_summary(gap) for gap in state.get("gap_intervals", [])],
        "adopted_count": state["adopted_count"],
        "rejected_count": state["rejected_count"],
        "discarded_count": state["discarded_count"],
        "last_event_seq": state.get("last_event_seq"),
        "last_sample_ms": state.get("last_sample_ms"),
        "keyframe_adoption": state.get("keyframe_adoption"),
        "adopted_increments_since_keyframe": list(state.get("adopted_increments_since_keyframe", [])),
        "breach_target_generation": state.get("breach_target_generation"),
    }


def rebuild_cursor_from_adoptions(
    adoptions: list[dict[str, Any]], through_event_seq: int | None = None
) -> dict[str, Any]:
    """从最近一次已采纳关键帧推导游标重建基线。

    这是代数切换规则的纯函数版本；流式 ``apply_generation_switch`` 与
    离线批量重建都调用它，因此两条路径不会产生两套规则。
    """

    eligible = [
        item
        for item in adoptions
        if item.get("decision") in {ADOPT_KEY, ADOPT_DELTA}
        and (through_event_seq is None or item["event_seq"] < through_event_seq)
    ]
    last_key_index = -1
    for index in range(len(eligible) - 1, -1, -1):
        if eligible[index]["decision"] == ADOPT_KEY:
            last_key_index = index
            break
    if last_key_index < 0:
        return {
            "keyframe": None,
            "baseline_cursor_seq": 0,
            "baseline_position_ms": None,
            "rewound_increments": [_increment_ref(item) for item in eligible],
        }
    keyframe = eligible[last_key_index]
    rewound = [_increment_ref(item) for item in eligible[last_key_index + 1 :]]
    return {
        "keyframe": {
            "event_seq": keyframe["event_seq"],
            "frame_seq": keyframe["frame_seq"],
            "sample_ms": keyframe["sample_ms"],
            "generation": keyframe.get("generation"),
        },
        "baseline_cursor_seq": keyframe["frame_seq"],
        "baseline_position_ms": keyframe["sample_ms"],
        "rewound_increments": rewound,
    }


def _increment_ref(item: dict[str, Any]) -> dict[str, Any]:
    return {
        "event_seq": item["event_seq"],
        "frame_seq": item["frame_seq"],
        "sample_ms": item["sample_ms"],
        "generation": item.get("generation"),
        "decision": item.get("decision", ADOPT_DELTA),
    }


def _close_gap(state: dict[str, Any], event_seq: int, sample_ms: int, reason: str) -> dict[str, Any] | None:
    gap = state.get("open_gap")
    if not gap:
        return None
    gap["status"] = "closed"
    gap["end_event_seq"] = event_seq
    gap["end_ms"] = sample_ms
    gap["end_reason"] = reason
    closed = _gap_summary(gap)
    state["gap_intervals"].append(gap)
    state["open_gap"] = None
    state["status"] = "ready"
    return closed


def _open_or_extend_gap(state: dict[str, Any], event: dict[str, Any], reason: str) -> dict[str, Any]:
    cursor = state["adoption_cursor_seq"]
    gap = state.get("open_gap")
    if gap is None:
        gap = {
            "gap_id": f"{state['receiver_id']}:gap:{event['event_seq']}",
            "status": "open",
            "start_event_seq": event["event_seq"],
            "start_ms": event["sample_ms"],
            "generation": state["generation"],
            "after_cursor_seq": cursor,
            "expected_frame_seq": cursor + 1,
            "observed_frame_seq": event["frame_seq"],
            "missing_frame_seqs": list(range(cursor + 1, event["frame_seq"])),
            "rejected_frame_seqs": [],
            "discarded_frame_seqs": [],
            "reason": reason,
        }
        state["open_gap"] = gap
        state["status"] = "waiting_gap"
    else:
        previous_observed = gap.get("observed_frame_seq", cursor)
        if event["frame_seq"] > previous_observed:
            gap["missing_frame_seqs"].extend(
                seq
                for seq in range(previous_observed + 1, event["frame_seq"])
                if seq not in gap["missing_frame_seqs"]
            )
        gap["observed_frame_seq"] = event["frame_seq"]
        gap["reason"] = reason if gap.get("reason") == reason else f"{gap.get('reason')}|{reason}"

    if event["frame_type"] == "discard":
        if event["frame_seq"] not in gap["discarded_frame_seqs"]:
            gap["discarded_frame_seqs"].append(event["frame_seq"])
    elif event["frame_seq"] not in gap["rejected_frame_seqs"]:
        gap["rejected_frame_seqs"].append(event["frame_seq"])
    return gap


def within_tolerance(state: dict[str, Any], event: dict[str, Any]) -> bool:
    tolerance = int(
        state.get("generation_tolerances", {}).get(str(state["generation"]), state.get("tolerance_ms", 500))
    )
    anchor = state["playback_position_ms"]
    delta = event["sample_ms"] - anchor
    return -tolerance <= delta <= tolerance


def apply_generation_switch(
    state: dict[str, Any],
    *,
    to_generation: int,
    switch_ms: int,
    event_seq: int,
    reason: str,
    adoptions: list[dict[str, Any]],
    new_tolerance_ms: int | None = None,
) -> dict[str, Any]:
    old_cursor = {
        "generation": state["generation"],
        "adoption_cursor_seq": state["adoption_cursor_seq"],
        "playback_position_ms": state["playback_position_ms"],
        "keyframe_seq": state["keyframe_seq"],
        "keyframe_ms": state["keyframe_ms"],
        "open_gap": _gap_summary(state.get("open_gap")),
    }
    closed_gap = _close_gap(state, event_seq, switch_ms, "generation_switch")
    rebuild = rebuild_cursor_from_adoptions(adoptions, through_event_seq=event_seq)
    keyframe = rebuild["keyframe"]
    discarded_increments = list(rebuild["rewound_increments"])
    if closed_gap:
        for frame_seq in closed_gap.get("rejected_frame_seqs", []):
            discarded_increments.append(
                {"frame_seq": frame_seq, "generation": old_cursor["generation"], "reason": "rejected_before_switch"}
            )
        for frame_seq in closed_gap.get("discarded_frame_seqs", []):
            discarded_increments.append(
                {"frame_seq": frame_seq, "generation": old_cursor["generation"], "reason": "declared_discard"}
            )
    discarded_increments.sort(key=lambda item: (item.get("event_seq", 1 << 62), item.get("frame_seq", 0)))

    state["generation"] = to_generation
    state["breach_target_generation"] = None
    if new_tolerance_ms is not None:
        state["tolerance_ms"] = new_tolerance_ms
        state["generation_tolerances"][str(to_generation)] = new_tolerance_ms
    if keyframe:
        state["adoption_cursor_seq"] = keyframe["frame_seq"]
        state["playback_position_ms"] = keyframe["sample_ms"]
        state["keyframe_seq"] = keyframe["frame_seq"]
        state["keyframe_ms"] = keyframe["sample_ms"]
    else:
        state["adoption_cursor_seq"] = 0
        state["playback_position_ms"] = state["start_ms"]
    state["status"] = "switched_rebuilding"
    state["pending_rebuild_id"] = f"rebuild:{state['receiver_id']}:{event_seq}"
    state["adopted_increments_since_keyframe"] = []

    report = {
        "rebuild_id": state["pending_rebuild_id"],
        "receiver_id": state["receiver_id"],
        "trigger_event_seq": event_seq,
        "trigger_logical_ms": switch_ms,
        "trigger_reason": reason,
        "from_generation": old_cursor["generation"],
        "to_generation": to_generation,
        "old_cursor": old_cursor,
        "recovery_keyframe": keyframe,
        "new_baseline": keyframe,
        "new_baseline_status": "recovery_keyframe_provisional",
        "discarded_increments": discarded_increments,
        "closed_cross_switch_gap": closed_gap,
        "state_after": public_state(state),
    }
    state["_last_rebuild_report"] = report
    return report


def _trace_record(event: dict[str, Any], decision: str, state: dict[str, Any], reasons: list[str]) -> dict[str, Any]:
    return {
        "event_seq": event["event_seq"],
        "record_type": event["record_type"],
        "session_id": event.get("session_id"),
        "receiver_id": event["receiver_id"],
        "frame_seq": event.get("frame_seq"),
        "sample_ms": event["sample_ms"],
        "generation": state["generation"],
        "decision": decision,
        "reasons": reasons,
        "state": public_state(state),
    }


def replay_receiver(
    receiver_id: str,
    events: list[dict[str, Any]],
    initial_state: dict[str, Any] | None = None,
    *,
    auto_switch_on_breach: bool = True,
) -> dict[str, Any]:
    """逐帧串行推进单个接收端状态机。"""

    state = deepcopy(initial_state) if initial_state else new_receiver_state(receiver_id)
    state.pop("_last_rebuild_report", None)
    state["receiver_id"] = receiver_id
    ordered = sorted(events, key=lambda event: event["event_seq"])
    adoptions: list[dict[str, Any]] = []
    if state.get("keyframe_adoption"):
        adoptions.append(deepcopy(state["keyframe_adoption"]))
        adoptions.extend(deepcopy(state.get("adopted_increments_since_keyframe", [])))
    trace: list[dict[str, Any]] = []
    rebuilds: list[dict[str, Any]] = []

    for event in ordered:
        reasons: list[str] = []
        if event["record_type"] == "switch":
            if event.get("from_generation") and event["from_generation"] != state["generation"]:
                reasons.append("marker_from_generation_mismatch_used_current")
            report = apply_generation_switch(
                state,
                to_generation=event["to_generation"],
                switch_ms=event["sample_ms"],
                event_seq=event["event_seq"],
                reason=event.get("reason", "explicit_marker"),
                adoptions=adoptions,
                new_tolerance_ms=event.get("tolerance_ms"),
            )
            rebuilds.append(report)
            trace.append(_trace_record(event, SWITCH, state, [report["trigger_reason"]]))
            continue

        event_generation = event["generation"]
        if event_generation != state["generation"]:
            report = apply_generation_switch(
                state,
                to_generation=event_generation,
                switch_ms=event["sample_ms"],
                event_seq=event["event_seq"],
                reason="frame_generation_changed",
                adoptions=adoptions,
            )
            rebuilds.append(report)

        time_ok = within_tolerance(state, event)
        continuous = event["frame_seq"] == state["adoption_cursor_seq"] + 1
        has_baseline = state["keyframe_seq"] > 0

        if event["frame_type"] == "key":
            closed_gap = _close_gap(state, event["event_seq"], event["sample_ms"], "keyframe")
            if closed_gap:
                reasons.append("closed_gap_by_keyframe")
            interim_rejected: list[dict[str, Any]] = []
            if closed_gap and state.get("pending_rebuild_id"):
                interim_rejected = [
                    {"frame_seq": seq, "reason": "post_switch_gap_rejected"}
                    for seq in closed_gap.get("rejected_frame_seqs", [])
                ]
                interim_rejected.extend(
                    {"frame_seq": seq, "reason": "post_switch_declared_discard"}
                    for seq in closed_gap.get("discarded_frame_seqs", [])
                )
            state["adoption_cursor_seq"] = event["frame_seq"]
            state["keyframe_seq"] = event["frame_seq"]
            state["keyframe_ms"] = event["sample_ms"]
            state["keyframe_generation"] = event_generation
            state["playback_position_ms"] = event["sample_ms"]
            state["last_event_seq"] = event["event_seq"]
            state["last_sample_ms"] = event["sample_ms"]
            state["status"] = "ready"
            state["adopted_count"] += 1
            adoption = _increment_ref({**event, "decision": ADOPT_KEY})
            adoptions.append({**event, "decision": ADOPT_KEY})
            state["keyframe_adoption"] = {**event, "decision": ADOPT_KEY}
            state["adopted_increments_since_keyframe"] = []
            if state.get("pending_rebuild_id"):
                for report in reversed(rebuilds):
                    if report["rebuild_id"] == state["pending_rebuild_id"]:
                        report["new_baseline"] = {
                            "event_seq": event["event_seq"],
                            "frame_seq": event["frame_seq"],
                            "sample_ms": event["sample_ms"],
                            "generation": event_generation,
                        }
                        report["new_baseline_status"] = "new_generation_keyframe"
                        if interim_rejected:
                            report["post_switch_discarded_increments"] = interim_rejected
                        report["state_after"] = public_state(state)
                        break
                state["pending_rebuild_id"] = None
            trace.append(_trace_record(event, ADOPT_KEY, state, reasons))
            continue

        if event["frame_type"] == "discard":
            state["discarded_count"] += 1
            state["rejected_count"] += 1
            _open_or_extend_gap(state, event, "declared_frame_discard")
            state["last_event_seq"] = event["event_seq"]
            state["last_sample_ms"] = event["sample_ms"]
            trace.append(_trace_record(event, DISCARD, state, ["frame_type=discard"]))
            continue

        if not has_baseline:
            reasons.append("no_keyframe_baseline")
        if not continuous:
            reasons.append("cursor_not_continuous")
        if not time_ok:
            reasons.append("sample_outside_tolerance")

        if not time_ok and auto_switch_on_breach:
            target_generation = state.get("breach_target_generation")
            if isinstance(target_generation, int) and target_generation > state["generation"]:
                report = apply_generation_switch(
                    state,
                    to_generation=target_generation,
                    switch_ms=event["sample_ms"],
                    event_seq=event["event_seq"],
                    reason="tolerance_window_exceeded",
                    adoptions=adoptions,
                )
                rebuilds.append(report)
                reasons.append("generation_switched_after_tolerance_breach")
                continuous = event["frame_seq"] == state["adoption_cursor_seq"] + 1

        if has_baseline and continuous and time_ok:
            state["adoption_cursor_seq"] = event["frame_seq"]
            state["playback_position_ms"] = event["sample_ms"]
            state["last_event_seq"] = event["event_seq"]
            state["last_sample_ms"] = event["sample_ms"]
            state["status"] = "ready"
            state["adopted_count"] += 1
            adoptions.append({**event, "decision": ADOPT_DELTA})
            state.setdefault("adopted_increments_since_keyframe", []).append(
                {**event, "decision": ADOPT_DELTA}
            )
            trace.append(_trace_record(event, ADOPT_DELTA, state, ["continuous_and_within_tolerance"]))
        else:
            state["rejected_count"] += 1
            gap_reason = "outside_tolerance" if not time_ok else "missing_or_noncontinuous_frame"
            if not has_baseline:
                gap_reason = "await_keyframe_baseline"
            _open_or_extend_gap(state, event, gap_reason)
            state["last_event_seq"] = event["event_seq"]
            state["last_sample_ms"] = event["sample_ms"]
            trace.append(_trace_record(event, GAP_WAIT, state, reasons))

    return {
        "receiver_id": receiver_id,
        "state": public_state(state),
        "adoptions": [_increment_ref(item) if "decision" in item and "event_seq" in item else item for item in adoptions],
        "trace": trace,
        "rebuilds": rebuilds,
        "buffer_bytes": sum(len(canonical(event)) for event in ordered),
    }


def replay_all_serial(
    events: list[dict[str, Any]],
    initial_states: dict[str, dict[str, Any]] | None = None,
    **options: Any,
) -> dict[str, dict[str, Any]]:
    grouped: dict[str, list[dict[str, Any]]] = {}
    for event in events:
        grouped.setdefault(event["receiver_id"], []).append(event)
    results = {}
    for receiver_id in sorted(grouped):
        initial = (initial_states or {}).get(receiver_id)
        results[receiver_id] = replay_receiver(receiver_id, grouped[receiver_id], initial, **options)
    return results
