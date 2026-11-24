"""按会话代数与迟到容忍窗口的帧采纳裁决.

裁决仅依赖事件到达顺序与每个接收屏的当前状态, 轨迹完全可复现:
  - 事件代数 < 当前代数: 触发代数回退, 重建该接收屏自最近关键帧起的播放状态;
  - 采样时刻早于 当前播放位置 - 迟到容忍窗口: 迟到丢弃;
  - 其余: 采纳并推进播放状态, 关键帧到达时重置解码基线。
"""

from __future__ import annotations

from dataclasses import dataclass, asdict

ADOPT = "adopt"
LATE_DROP = "late_drop"
ROLLBACK = "rollback"


@dataclass
class ScreenState:
    screen_id: str
    generation: int
    position_ms: int          # 当前播放位置(绝对逻辑毫秒)
    keyframe_seq: int         # 最近关键帧序列号
    decoded_frame_no: int     # 已解码帧序号
    last_keyframe_ms: int     # 最近关键帧的采样时刻(解码基线)
    tolerance_ms: int         # 迟到容忍窗口
    tombstone: bool = False

    def to_dict(self) -> dict:
        return asdict(self)


def zero_state(screen_id: str, generation: int, tolerance_ms: int) -> ScreenState:
    return ScreenState(
        screen_id=screen_id,
        generation=generation,
        position_ms=0,
        keyframe_seq=0,
        decoded_frame_no=0,
        last_keyframe_ms=0,
        tolerance_ms=tolerance_ms,
        tombstone=False,
    )


def decide(state: ScreenState, event: dict) -> str:
    """对单个帧事件就地裁决并推进 state, 返回裁决结果."""
    ev_gen = int(event["generation"])
    sample_ms = int(event["sample_ms"])

    if ev_gen < state.generation:
        # 代数回退: 重建自最近关键帧起的播放状态
        state.generation = ev_gen
        state.position_ms = state.last_keyframe_ms
        state.decoded_frame_no = state.keyframe_seq
        _adopt(state, event)
        return ROLLBACK

    if sample_ms < state.position_ms - state.tolerance_ms:
        return LATE_DROP

    if ev_gen > state.generation:
        state.generation = ev_gen
    _adopt(state, event)
    return ADOPT


def _adopt(state: ScreenState, event: dict) -> None:
    state.position_ms = int(event["sample_ms"])
    state.decoded_frame_no = int(event["frame_no"])
    if event.get("keyframe"):
        # 关键帧到达: 重置解码基线
        state.keyframe_seq = int(event["frame_no"])
        state.last_keyframe_ms = int(event["sample_ms"])
