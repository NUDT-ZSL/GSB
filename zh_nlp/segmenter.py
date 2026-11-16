"""基于词典的正向最大匹配分词器。"""
from __future__ import annotations

import math

from .config import PipelineConfig
from .data import Lexicon
from .numeric import NEG_INF, round_score


class MaximumMatchingSegmenter:
    def __init__(self, lexicon: Lexicon, config: PipelineConfig):
        self.lexicon = lexicon
        self.config = config

    def _candidates_at(self, text: str, start: int) -> list[str]:
        """返回从 start 起的全部词典候选词（已按裁决规则排序）。"""
        upper = min(len(text), start + self.config.max_word_length)
        candidates = [
            text[start:end]
            for end in range(start + 1, upper + 1)
            if text[start:end] in self.lexicon.words
        ]
        # 长度降序；等长按词频降序、词形字典序升序。
        candidates.sort(
            key=lambda word: (
                -len(word),
                -self.lexicon.lookup(word),
                word,
            )
        )
        return candidates

    def segment(self, sentence_id: str, text: str) -> dict:
        tokens: list[dict] = []
        position = 0
        total_loss = 0.0
        while position < len(text):
            candidates = self._candidates_at(text, position)
            if candidates:
                word = candidates[0]
                frequency_sum = sum(
                    self.lexicon.lookup(candidate) for candidate in candidates
                )
                confidence = (
                    self.lexicon.lookup(word) / frequency_sum
                    if frequency_sum > 0 else 0.0
                )
                in_lexicon = True
            else:
                word = text[position]
                confidence = self.config.unknown_confidence
                in_lexicon = False
            end = position + len(word)
            token_loss = -math.log(confidence) if confidence > 0 else NEG_INF
            total_loss += token_loss if math.isfinite(token_loss) else 0.0
            tokens.append({
                "word": word,
                "start": position,
                "end": end,
                "in_lexicon": in_lexicon,
                "boundary_confidence": round_score(confidence),
                "segmentation_loss": round_score(token_loss),
            })
            position = end
        return {
            "sentence_id": sentence_id,
            "text": text,
            "tokens": tokens,
            "segmentation_log_loss": round_score(total_loss),
        }
