"""确定性样本生成器。

按 stream_id 与 seed 生成逐词事件清单，并把到达顺序打乱为若干乱序窗口。
同一种子逐字节可复现；不依赖系统时间（虚拟时钟从 0 推进）。
"""
from __future__ import annotations

import zlib
from typing import List, Tuple

import numpy as np

from .events import WordEvent, sort_events

ZH_WORDS = (
    "我们", "今天", "直播", "开始", "欢迎", "大家", "观看", "字幕", "系统",
    "正在", "测试", "实时", "转写", "效果", "非常", "稳定", "感谢", "关注",
    "接下来", "请看", "演示", "数据", "模型", "结果", "观众", "朋友",
)
EN_WORDS = (
    "hello", "live", "stream", "caption", "engine", "test", "word", "chunk",
    "render", "final", "temp", "line", "screen", "token", "audio", "signal",
    "latency", "budget", "rewrite", "version", "stable", "demo", "ok",
)


def derive_seed(stream_id: str, seed: int) -> int:
    """由 stream_id 与 seed 派生确定性种子（crc32 而非内置 hash，避免盐化）。"""
    return (zlib.crc32(stream_id.encode("utf-8")) ^ (seed & 0xFFFFFFFF)) & 0xFFFFFFFF


def generate_timeline(stream_id: str, seed: int, num_words: int,
                      zh_ratio: float = 0.4) -> List[WordEvent]:
    """生成严格按时间轴推进、word_index 连续的逐词事件（未打乱）。"""
    rng = np.random.default_rng(derive_seed(stream_id, seed))
    events: List[WordEvent] = []
    clock = 0
    for idx in range(num_words):
        is_zh = bool(rng.random() < zh_ratio)
        vocab = ZH_WORDS if is_zh else EN_WORDS
        word = vocab[int(rng.integers(0, len(vocab)))]
        duration = int(120 + rng.random() * 280)
        gap = int(rng.random() * 150)
        start_ms = clock
        end_ms = clock + duration
        confidence = round(float(0.5 + rng.random() * 0.5), 4)
        events.append(WordEvent(
            stream_id=stream_id, word_index=idx,
            start_ms=start_ms, end_ms=end_ms, word=word,
            confidence=confidence, lang="zh" if is_zh else "en",
        ))
        clock = end_ms + gap
    return events


def shuffle_arrival(events: List[WordEvent], seed: int, stream_id: str,
                    disorder_window: int, arrival_interval_ms: int = 40
                    ) -> List[WordEvent]:
    """把事件切成若干乱序窗口并在窗内确定性重排，生成到达顺序。

    arrival_seq 连续编号，arrival_ms 由虚拟时钟等间隔推进（确定性）。
    """
    rng = np.random.default_rng(derive_seed(stream_id + "#arrival", seed))
    ordered = sort_events(events)
    arrived: List[WordEvent] = []
    for base in range(0, len(ordered), max(1, disorder_window)):
        window = ordered[base:base + max(1, disorder_window)]
        perm = rng.permutation(len(window))
        for pos in perm:
            arrived.append(window[int(pos)])
    result: List[WordEvent] = []
    for seq, ev in enumerate(arrived):
        result.append(WordEvent(
            stream_id=ev.stream_id, word_index=ev.word_index,
            start_ms=ev.start_ms, end_ms=ev.end_ms, word=ev.word,
            confidence=ev.confidence, lang=ev.lang,
            arrival_seq=seq, arrival_ms=seq * arrival_interval_ms,
        ))
    return result


def generate_stream(stream_id: str, seed: int, num_words: int,
                    disorder_window: int = 6, arrival_interval_ms: int = 40
                    ) -> Tuple[List[WordEvent], List[WordEvent]]:
    """返回 (到达顺序事件, 时间轴顺序事件)。"""
    timeline = generate_timeline(stream_id, seed, num_words)
    arrived = shuffle_arrival(timeline, seed, stream_id,
                              disorder_window, arrival_interval_ms)
    return arrived, sort_events(timeline)
