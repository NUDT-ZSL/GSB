"""逐词转写事件模型与 JSONL 读写。

事件字段：stream_id, word_index, start_ms, end_ms, word, confidence, lang
附加确定性到达元数据：arrival_seq（到达序号）、arrival_ms（模拟到达时刻）。
所有排序均使用显式键，禁止依赖字典遍历顺序或系统时间。
"""
from __future__ import annotations

import json
from dataclasses import dataclass, asdict
from typing import Iterable, Iterator, List, Tuple

# 显式声明的同分打破规则：
# 1) start_ms 升序  2) word_index 升序  3) end_ms 升序  4) word 字典序
SORT_RULE = "start_ms ASC, word_index ASC, end_ms ASC, word ASC"

EVENT_FIELDS = (
    "stream_id", "word_index", "start_ms", "end_ms",
    "word", "confidence", "lang", "arrival_seq", "arrival_ms",
)


@dataclass(frozen=True)
class WordEvent:
    stream_id: str
    word_index: int
    start_ms: int
    end_ms: int
    word: str
    confidence: float
    lang: str
    arrival_seq: int = 0
    arrival_ms: int = 0

    def sort_key(self) -> Tuple[int, int, int, str]:
        return (self.start_ms, self.word_index, self.end_ms, self.word)

    def to_dict(self) -> dict:
        return asdict(self)

    @staticmethod
    def from_dict(data: dict) -> "WordEvent":
        return WordEvent(
            stream_id=str(data["stream_id"]),
            word_index=int(data["word_index"]),
            start_ms=int(data["start_ms"]),
            end_ms=int(data["end_ms"]),
            word=str(data["word"]),
            confidence=float(data["confidence"]),
            lang=str(data["lang"]),
            arrival_seq=int(data.get("arrival_seq", 0)),
            arrival_ms=int(data.get("arrival_ms", 0)),
        )


def sort_events(events: Iterable[WordEvent]) -> List[WordEvent]:
    """按显式声明的同分打破规则排序。"""
    return sorted(events, key=lambda e: e.sort_key())


def dump_jsonl(events: Iterable[WordEvent], path: str) -> None:
    """逐字节确定性写出：固定字段顺序、紧凑分隔符、UTF-8、\\n 行尾。"""
    with open(path, "w", encoding="utf-8", newline="\n") as fh:
        for ev in events:
            obj = {k: ev.to_dict()[k] for k in EVENT_FIELDS}
            fh.write(json.dumps(obj, ensure_ascii=False, separators=(",", ":")))
            fh.write("\n")


def load_jsonl(path: str) -> List[WordEvent]:
    events: List[WordEvent] = []
    with open(path, "r", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if line:
                events.append(WordEvent.from_dict(json.loads(line)))
    return events


def iter_jsonl(path: str) -> Iterator[WordEvent]:
    with open(path, "r", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if line:
                yield WordEvent.from_dict(json.loads(line))
