"""HMM 未登录词识别：对数域维特比解码（BMES 状态）。"""
from __future__ import annotations

import math

import numpy as np

from .config import PipelineConfig
from .data import LogProbs
from .numeric import NEG_INF, round_score, stable_log_sum_exp


class HmmIdentifier:
    def __init__(self, model: LogProbs, config: PipelineConfig):
        self.model = model
        self.config = config
        self.smoothing = config.hmm_emission_smoothing
        self.log_smoothing = math.log(self.smoothing)

    def _emission_vector(self, char: str) -> np.ndarray:
        vector = self.model.emission.get(char)
        if vector is None:
            return np.full(
                len(self.model.states), self.log_smoothing, dtype=np.float64)
        return vector.copy()

    def decode(self, chars: list[str], positions: list[int]) -> dict:
        """对连续单字片段做维特比解码并按 BMES 重组词。"""
        count = len(self.model.states)
        length = len(chars)
        score = np.full((length, count), NEG_INF, dtype=np.float64)
        backpointer = np.full((length, count), -1, dtype=np.int64)

        first_emission = self._emission_vector(chars[0])
        score[0] = self.model.initial + first_emission
        for time in range(1, length):
            emission = self._emission_vector(chars[time])
            for target in range(count):
                predecessor = score[time - 1] + self.model.transition[:, target]
                best = int(np.argmax(predecessor))
                score[time, target] = predecessor[best] + emission[target]
                backpointer[time, target] = best

        best_final = int(np.argmax(score[length - 1]))
        best_path_score = float(score[length - 1, best_final])
        tags: list[int] = [0] * length
        tags[-1] = best_final
        for time in range(length - 1, 0, -1):
            tags[time - 1] = int(backpointer[time, tags[time]])

        # 前向算法在全部候选路径上做 log-sum-exp，得到整段归一化对数概率。
        forward = np.full((length, count), NEG_INF, dtype=np.float64)
        forward[0] = self.model.initial + first_emission
        for time in range(1, length):
            emission = self._emission_vector(chars[time])
            for target in range(count):
                column = forward[time - 1] + self.model.transition[:, target]
                forward[time, target] = (
                    stable_log_sum_exp(column) + emission[target])
        log_normalized = stable_log_sum_exp(forward[length - 1])

        tokens = self._assemble(chars, positions, tags)
        smoothed = [
            char for char in chars if char not in self.model.emission
        ]
        return {
            "tags": [self.model.states[index] for index in tags],
            "tokens": tokens,
            "viterbi_score": round_score(best_path_score),
            "log_normalized_probability": round_score(log_normalized),
            "smoothed_chars": smoothed,
        }

    def _assemble(
        self, chars: list[str], positions: list[int], tags: list[int]
    ) -> list[dict]:
        tag_names = self.model.states
        words: list[dict] = []
        start = 0
        for offset in range(len(chars)):
            tag = tag_names[tags[offset]]
            end_of_word = tag in ("E", "S") or offset == len(chars) - 1
            if end_of_word:
                word = "".join(chars[start:offset + 1])
                words.append({
                    "word": word,
                    "start": positions[start],
                    "end": positions[offset] + 1,
                    "hmm_tags": [tag_names[tags[i]]
                                 for i in range(start, offset + 1)],
                })
                start = offset + 1
        return words
