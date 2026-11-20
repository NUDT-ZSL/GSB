"""临时视图与完整重排结果的显示一致性裁决。

声明精度：逐行比较可见屏内每一行的词序列（word_index 序列）与词序，
完全一致视为一致；渲染标记（临时 ⟨word⟩）属显示层，不参与一致性口径。
"""
from __future__ import annotations

from typing import List, Sequence

from .config import DisplayPolicy
from .errors import E_VIEW_INCONSISTENT, BusinessError
from .events import WordEvent, sort_events
from .lines import Line, assemble_lines

PRECISION = "per-line word_index sequence equality within the visible screen"


def full_reorder_lines(received: Sequence[WordEvent], policy: DisplayPolicy,
                       stream_id: str, final: bool = False) -> List[Line]:
    """用检查点已接收事件按完整重排口径计算行内容。"""
    ordered = sort_events(received)
    return assemble_lines(ordered, policy, stream_id=stream_id,
                          chunk_seq=-1, final=final)


def check_view_consistency(visible: Sequence[Line],
                           received: Sequence[WordEvent],
                           policy: DisplayPolicy, stream_id: str,
                           checkpoint: str) -> None:
    """屏幕可见行必须与完整重排口径的尾部可见窗口一致。

    可见屏 = 完整重排结果的最后 max_lines_per_screen 行。
    """
    expected_all = full_reorder_lines(received, policy, stream_id)
    expected = expected_all[len(expected_all) - policy.max_lines_per_screen:]
    actual_keys = [ln.word_indices for ln in visible]
    expected_keys = [ln.word_indices for ln in expected]
    if actual_keys == expected_keys:
        return
    first_bad = 0
    left = right = ""
    actual_texts = [ln.text for ln in visible]
    expected_texts = [ln.text for ln in expected]
    for i in range(max(len(actual_keys), len(expected_keys))):
        a = actual_keys[i] if i < len(actual_keys) else ()
        b = expected_keys[i] if i < len(expected_keys) else ()
        if a != b:
            first_bad = i
            left = actual_texts[i] if i < len(actual_texts) else "<missing>"
            right = expected_texts[i] if i < len(expected_texts) else "<missing>"
            break
    raise BusinessError(
        E_VIEW_INCONSISTENT,
        f"visible view diverges from full-reorder view at line {first_bad}",
        checkpoint=checkpoint, first_divergent_line=first_bad,
        visible_line=left, recomputed_line=right,
        precision=PRECISION,
    )
