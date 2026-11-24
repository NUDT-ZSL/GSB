"""重放裁决校验与偏差定位.

把帧事件流按逻辑时刻逐条重放, 与合并后的快照逐接收屏比对
关键帧序号、播放位置与会话代数; 发现偏差时定位首个不一致的
逻辑时刻、接收屏标识与字段, 产出带 E_REPLAY 业务码的 JSON 报告;
校验通过时产出含统计信息的通过报告。
"""

from __future__ import annotations

from .adjudicate import zero_state
from .errors import E_REPLAY
from .replay import replay_events, sort_events

COMPARE_FIELDS = ("keyframe_seq", "position_ms", "generation")


def build_initial_states(view: dict) -> dict:
    """由合并视图构造重放基线: 零状态 + 视图中的代数与迟到容忍窗口."""
    initial = {}
    for screen_id, rec in sorted(view.items()):
        st = zero_state(
            screen_id,
            generation=int(rec["generation"]),
            tolerance_ms=int(rec["tolerance_ms"] or 0),
        )
        st.tombstone = bool(rec["tombstone"])
        initial[screen_id] = st
    return initial


def _first_divergence(trace: list[dict], screen_id: str, field: str, expected) -> dict:
    """定位首个不一致逻辑时刻: 该字段从此刻起不再回到期望值."""
    entries = [t for t in trace if t["screen_id"] == screen_id]
    for idx, entry in enumerate(entries):
        if entry["state"][field] == expected:
            continue
        if all(e["state"][field] != expected for e in entries[idx:]):
            return {
                "screen_id": screen_id,
                "field": field,
                "first_divergent_ms": entry["sample_ms"],
                "event_seq": entry["seq"],
                "expected": expected,
                "actual": entry["state"][field],
            }
    return {
        "screen_id": screen_id,
        "field": field,
        "first_divergent_ms": None,
        "event_seq": None,
        "expected": expected,
        "actual": None,
    }


def verify_replay(view: dict, events: list[dict]) -> dict:
    """重放事件流并与合并视图比对, 返回校验报告(不抛异常)."""
    ordered = sort_events(events)
    initial = build_initial_states(view)
    states, trace, counts = replay_events(ordered, initial)
    divergences = []
    compared = 0
    for screen_id, rec in sorted(view.items()):
        if rec["tombstone"]:
            continue  # 墓碑接收屏不参与比对
        compared += 1
        final = states[screen_id]
        expected_map = {
            "keyframe_seq": int(rec["keyframe_seq"]),
            "position_ms": int(rec["ts_ms"]),
            "generation": int(rec["generation"]),
        }
        for field in COMPARE_FIELDS:
            actual = getattr(final, field)
            if actual != expected_map[field]:
                divergences.append(
                    _first_divergence(trace, screen_id, field, expected_map[field])
                )
    stats = {
        "events_total": len(ordered),
        "screens_compared": compared,
        "screens_tombstoned": sum(1 for r in view.values() if r["tombstone"]),
        "adopt": counts["adopt"],
        "late_drop": counts["late_drop"],
        "rollback": counts["rollback"],
    }
    if divergences:
        divergences.sort(
            key=lambda d: (
                d["first_divergent_ms"] if d["first_divergent_ms"] is not None else 1 << 62,
                d["screen_id"], d["field"],
            )
        )
        return {"ok": False, "code": E_REPLAY,
                "message": "重放结果与合并快照不一致",
                "divergences": divergences, "stats": stats}
    return {"ok": True, "code": None,
            "message": "重放结果与合并快照一致",
            "divergences": [], "stats": stats}
