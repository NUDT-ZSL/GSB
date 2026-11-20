"""确定性演示场景。

每个场景返回 (到达顺序事件, 显示策略, 场景说明)，全部由固定种子生成，
同参数逐字节可复现。
"""
from __future__ import annotations

from typing import List, Tuple

from .config import DisplayPolicy
from .events import WordEvent
from .generator import generate_stream


def _retime(events: List[WordEvent], interval_ms: int) -> List[WordEvent]:
    """按到达顺序重设虚拟到达时钟（保持确定性）。"""
    return [WordEvent(e.stream_id, e.word_index, e.start_ms, e.end_ms,
                      e.word, e.confidence, e.lang, e.arrival_seq,
                      seq * interval_ms)
            for seq, e in enumerate(events)]


def scenario_normal(seed: int = 42) -> Tuple[List[WordEvent], DisplayPolicy, str]:
    arrived, _ = generate_stream("live-normal", seed, num_words=60,
                                 disorder_window=6, arrival_interval_ms=40)
    return arrived, DisplayPolicy(), "常规乱序流：偶发迟到，稳定收敛"


def scenario_late_rollback(seed: int = 7) -> Tuple[List[WordEvent], DisplayPolicy, str]:
    arrived, _ = generate_stream("live-late", seed, num_words=48,
                                 disorder_window=8, arrival_interval_ms=40)
    policy = DisplayPolicy(max_wait_ms=120, rollback_steps=8)
    return arrived, policy, "迟到事件回滚：短等待触发版本化改写"


def scenario_latency_breach(seed: int = 11) -> Tuple[List[WordEvent], DisplayPolicy, str]:
    arrived, _ = generate_stream("live-latency", seed, num_words=40,
                                 disorder_window=4, arrival_interval_ms=40)
    arrived = _retime(arrived, interval_ms=600)  # 到达稀疏，定稿等待超预算
    policy = DisplayPolicy(max_wait_ms=400, rollback_steps=16)
    return arrived, policy, "超延迟演示：到达稀疏导致定稿等待突破预算"


def scenario_line_width(seed: int = 3) -> Tuple[List[WordEvent], DisplayPolicy, str]:
    arrived, _ = generate_stream("live-width", seed, num_words=24,
                                 disorder_window=4, arrival_interval_ms=40)
    ev = arrived[10]
    arrived[10] = WordEvent(ev.stream_id, ev.word_index, ev.start_ms,
                            ev.end_ms, "x" * 40, ev.confidence, ev.lang,
                            ev.arrival_seq, ev.arrival_ms)
    return arrived, DisplayPolicy(), "异常注入：超长词触发 E_LINE_WIDTH"


def scenario_word_dup(seed: int = 5) -> Tuple[List[WordEvent], DisplayPolicy, str]:
    arrived, _ = generate_stream("live-dup", seed, num_words=24,
                                 disorder_window=4, arrival_interval_ms=40)
    ev = arrived[20]
    arrived[20] = WordEvent(ev.stream_id, arrived[0].word_index, ev.start_ms,
                            ev.end_ms, ev.word, ev.confidence, ev.lang,
                            ev.arrival_seq, ev.arrival_ms)
    return arrived, DisplayPolicy(), "异常注入：重复 word_index 触发 E_WORD_DUP"


def scenario_word_missing(seed: int = 9) -> Tuple[List[WordEvent], DisplayPolicy, str]:
    arrived, _ = generate_stream("live-missing", seed, num_words=24,
                                 disorder_window=4, arrival_interval_ms=40)
    arrived = [e for e in arrived if e.word_index != 10]  # 制造确定性缺口
    return arrived, DisplayPolicy(), "异常注入：缺失事件触发 E_WORD_MISSING"


def scenario_rewrite_depth(seed: int = 13) -> Tuple[List[WordEvent], DisplayPolicy, str]:
    arrived, _ = generate_stream("live-depth", seed, num_words=36,
                                 disorder_window=8, arrival_interval_ms=40)
    policy = DisplayPolicy(max_wait_ms=80, rollback_steps=1)
    return arrived, policy, "异常注入：回滚步数=1 触发 E_REWRITE_DEPTH"


def scenario_resource_limit(seed: int = 17) -> Tuple[List[WordEvent], DisplayPolicy, str]:
    arrived, _ = generate_stream("live-resource", seed, num_words=40,
                                 disorder_window=4, arrival_interval_ms=40)
    policy = DisplayPolicy(max_events_per_stream=20)
    return arrived, policy, "异常注入：事件总数上限触发 E_RESOURCE_LIMIT"


SCENARIOS = {
    "normal": scenario_normal,
    "late_rollback": scenario_late_rollback,
    "latency_breach": scenario_latency_breach,
    "line_width": scenario_line_width,
    "word_dup": scenario_word_dup,
    "word_missing": scenario_word_missing,
    "rewrite_depth": scenario_rewrite_depth,
    "resource_limit": scenario_resource_limit,
}
