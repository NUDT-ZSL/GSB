"""词性标注：对数域维特比 + log-sum-exp 全路径归一化。"""
from __future__ import annotations

import math

import numpy as np

from .config import PipelineConfig
from .data import LogProbs
from .numeric import NEG_INF, round_score, stable_log_sum_exp


class PosTagger:
    def __init__(self, model: LogProbs, config: PipelineConfig):
        self.model = model
        self.config = config
        self.emission_log_smoothing = math.log(
            config.pos_emission_smoothing)
        self.transition_log_smoothing = math.log(
            config.pos_transition_smoothing)

    def _emission_vector(self, word: str) -> tuple[np.ndarray, bool]:
        vector = self.model.emission.get(word)
        if vector is None:
            return (
                np.full(len(self.model.states),
                        self.emission_log_smoothing, dtype=np.float64),
                True,
            )
        return vector.copy(), False

    def _transition_matrix(self) -> np.ndarray:
        matrix = self.model.transition.copy()
        matrix[~np.isfinite(matrix)] = self.transition_log_smoothing
        return matrix

    def tag(self, tokens: list[dict]) -> dict:
        count = len(self.model.states)
        length = len(tokens)
        transition = self._transition_matrix()

        emissions: list[np.ndarray] = []
        smoothed_words: list[str] = []
        for token in tokens:
            vector, smoothed = self._emission_vector(token["word"])
            emissions.append(vector)
            if smoothed:
                smoothed_words.append(token["word"])

        # 维特比：逐元素加法 + 最优前驱比较，全程不做概率累乘。
        score = np.full((length, count), NEG_INF, dtype=np.float64)
        backpointer = np.full((length, count), -1, dtype=np.int64)
        score[0] = self.model.initial + emissions[0]
        for time in range(1, length):
            for target in range(count):
                predecessor = score[time - 1] + transition[:, target]
                best = int(np.argmax(predecessor))
                score[time, target] = predecessor[best] + emissions[time][target]
                backpointer[time, target] = best

        best_final = int(np.argmax(score[length - 1]))
        tags: list[int] = [0] * length
        tags[-1] = best_final
        for time in range(length - 1, 0, -1):
            tags[time - 1] = int(backpointer[time, tags[time]])

        # 前向全路径 log-sum-exp，作为整句归一化对数概率。
        forward = np.full((length, count), NEG_INF, dtype=np.float64)
        forward[0] = self.model.initial + emissions[0]
        for time in range(1, length):
            for target in range(count):
                column = forward[time - 1] + transition[:, target]
                forward[time, target] = (
                    stable_log_sum_exp(column) + emissions[time][target])
        log_normalized = stable_log_sum_exp(forward[length - 1])
        total_path_score = float(score[length - 1, best_final])

        threshold = self.config.low_confidence_threshold
        tagged: list[dict] = []
        pos_loss = 0.0
        low_confidence_count = 0
        for time, token in enumerate(tokens):
            tag = self.model.states[tags[time]]
            local_score = float(score[time, tags[time]])
            seg_confidence = float(token["boundary_confidence"])
            seg_log = (
                math.log(seg_confidence) if seg_confidence > 0 else NEG_INF
            )
            low_seg = math.isfinite(seg_log) and seg_log < threshold
            low_pos = local_score < threshold
            if low_pos:
                low_confidence_count += 1
            tagged.append({
                "word": token["word"],
                "start": token["start"],
                "end": token["end"],
                "pos": tag,
                "local_path_score": round_score(local_score),
                "boundary_confidence": round_score(seg_confidence),
                "low_confidence": low_seg or low_pos,
                "low_confidence_stage": (
                    ("segmentation,pos" if low_seg and low_pos else
                     "segmentation" if low_seg else
                     "pos" if low_pos else None)
                ),
                "triggered_threshold": round_score(threshold)
                if low_seg or low_pos else None,
            })
            pos_loss += -local_score

        return {
            "tagged_tokens": tagged,
            "tags": [self.model.states[index] for index in tags],
            "total_path_score": round_score(total_path_score),
            "log_normalized_probability": round_score(log_normalized),
            "pos_log_loss": round_score(pos_loss),
            "low_confidence_count": low_confidence_count,
            "smoothed_words": smoothed_words,
        }
