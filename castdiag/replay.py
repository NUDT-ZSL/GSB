"""确定性帧重放与状态演进.

同一事件流与同一初始快照重复重放, 必得到逐接收屏逐字段相同的
状态序列与相同采纳计数(不依赖事件到达顺序之外的任何状态)。
"""

from __future__ import annotations

from .adjudicate import ADOPT, LATE_DROP, ROLLBACK, ScreenState, decide


def replay_events(events: list[dict], initial_states: dict[str, ScreenState]):
    """按给定顺序重放事件, 返回 (终态, 裁决轨迹, 采纳计数)."""
    states = {sid: ScreenState(**st.to_dict()) for sid, st in initial_states.items()}
    trace: list[dict] = []
    counts = {ADOPT: 0, LATE_DROP: 0, ROLLBACK: 0}
    for ev in events:
        sid = ev["screen_id"]
        if sid not in states:
            continue  # 未在合并视图中的接收屏(如已墓碑移除)不参与重放
        state = states[sid]
        if state.tombstone:
            continue
        decision = decide(state, ev)
        counts[decision] += 1
        trace.append(
            {
                "seq": int(ev["seq"]),
                "sample_ms": int(ev["sample_ms"]),
                "screen_id": sid,
                "decision": decision,
                "state": state.to_dict(),
            }
        )
    return states, trace, counts


def sort_events(events: list[dict]) -> list[dict]:
    """按可注入逻辑时钟给出的到达顺序(事件序号)逐条重放.

    事件序号即逻辑时钟的到达次序; 迟到判定依赖到达顺序,
    因此重放顺序与裁决轨迹均以事件序号为准, 保证可复现。
    """
    return sorted(events, key=lambda e: int(e["seq"]))
