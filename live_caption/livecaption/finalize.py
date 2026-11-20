"""收敛保证与定稿校验。

- 定稿 = 全部事件按时间轴全局排序后一次性重排装配（final 渲染口径）；
- 校验每个 word_index 恰出现一次（E_WORD_DUP / E_WORD_MISSING）；
- 校验定稿时间轴单调不重叠（E_TIMELINE_ORDER）；
- 引擎增量产出的定稿文本必须与一次性全局重排结果逐字符相同。
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import List, Sequence

from .config import DisplayPolicy
from .errors import (E_TIMELINE_ORDER, E_WORD_DUP, E_WORD_MISSING,
                     BusinessError)
from .events import WordEvent, sort_events
from .lines import Line, assemble_lines


@dataclass(frozen=True)
class FinalResult:
    lines: List[Line]
    text: str
    chunk_count: int
    rewrite_total: int


def validate_word_indices(events: Sequence[WordEvent], stream_id: str) -> None:
    """每个 word_index 恰出现一次且覆盖 [0, n)。"""
    seen = {}
    for ev in events:
        if ev.word_index in seen:
            raise BusinessError(
                E_WORD_DUP,
                f"duplicate word_index {ev.word_index}",
                stream_id=stream_id, word_index=ev.word_index,
                first=seen[ev.word_index], second=ev.word,
            )
        seen[ev.word_index] = ev.word
    n = len(events)
    for idx in range(n):
        if idx not in seen:
            raise BusinessError(
                E_WORD_MISSING,
                f"missing word_index {idx}",
                stream_id=stream_id, word_index=idx, expected_count=n,
            )


def validate_timeline(events: Sequence[WordEvent], stream_id: str) -> None:
    """定稿时间轴单调不重叠：start_ms 单调且相邻区间不重叠。"""
    prev = None
    for ev in events:
        if prev is not None:
            if ev.start_ms < prev.start_ms or ev.start_ms < prev.end_ms:
                raise BusinessError(
                    E_TIMELINE_ORDER,
                    f"timeline not monotonic/non-overlapping at "
                    f"word_index {ev.word_index}",
                    stream_id=stream_id, word_index=ev.word_index,
                    prev_end_ms=prev.end_ms, start_ms=ev.start_ms,
                )
        prev = ev


def finalize_once(events: Sequence[WordEvent], policy: DisplayPolicy,
                  stream_id: str, chunk_count: int = 0,
                  rewrite_total: int = 0) -> FinalResult:
    """一次性全局重排定稿：排序 -> 校验 -> 装配（final 口径）。"""
    validate_word_indices(events, stream_id)
    ordered = sort_events(events)
    validate_timeline(ordered, stream_id)
    lines = assemble_lines(ordered, policy, stream_id=stream_id,
                           chunk_seq=-1, final=True)
    return FinalResult(lines=lines, text="\n".join(ln.text for ln in lines),
                       chunk_count=chunk_count, rewrite_total=rewrite_total)


def check_convergence(incremental_text: str, oneshot_text: str) -> bool:
    """增量定稿与一次性全局重排结果逐字符比较。"""
    return incremental_text == oneshot_text
