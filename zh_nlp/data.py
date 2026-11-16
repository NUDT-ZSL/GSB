"""词典与概率表加载：原始概率在加载时统一转换为自然对数域。"""
from __future__ import annotations

import json
from pathlib import Path

import numpy as np

from .numeric import NEG_INF, log_safe


class Lexicon:
    """词 -> 词频；重复词形保留词频较大者。"""

    def __init__(self, path: str | Path):
        self.words: dict[str, int] = {}
        with open(path, "r", encoding="utf-8") as handle:
            for line_no, raw in enumerate(handle, 1):
                line = raw.strip()
                if not line:
                    continue
                parts = line.split()
                if len(parts) != 2:
                    raise ValueError(
                        f"词典第 {line_no} 行格式错误，应为“词 词频”: {raw!r}")
                word, freq_text = parts
                frequency = int(freq_text)
                if frequency < 0:
                    raise ValueError(f"词典第 {line_no} 行词频为负: {word}")
                if frequency > self.words.get(word, -1):
                    self.words[word] = frequency

    def lookup(self, word: str) -> int:
        return self.words.get(word, 0)


class LogProbs:
    """HMM 或词性标注概率表的对数域表示。

    states 顺序固定（字典序），所有矩阵按该顺序索引，保证可复现。
    """

    def __init__(self, path: str | Path):
        with open(path, "r", encoding="utf-8") as handle:
            raw = json.load(handle)
        self.states: tuple[str, ...] = tuple(sorted(raw["states"]))
        index = {state: i for i, state in enumerate(self.states)}
        count = len(self.states)

        self.initial = np.full(count, NEG_INF, dtype=np.float64)
        for state, probability in raw["initial_probs"].items():
            self.initial[index[state]] = log_safe(float(probability))

        self.transition = np.full((count, count), NEG_INF, dtype=np.float64)
        for source, targets in raw["transition_probs"].items():
            for target, probability in targets.items():
                self.transition[index[source], index[target]] = log_safe(
                    float(probability))

        emission_raw = raw["emission_probs"]
        self.emission: dict[str, np.ndarray] = {}
        for observation, state_probs in emission_raw.items():
            vector = np.full(count, NEG_INF, dtype=np.float64)
            for state, probability in state_probs.items():
                vector[index[state]] = log_safe(float(probability))
            self.emission[observation] = vector

        missing = [
            state for state in self.states
            if state not in raw["initial_probs"]
        ]
        if missing:
            raise ValueError(f"初始概率缺少状态: {missing}")
