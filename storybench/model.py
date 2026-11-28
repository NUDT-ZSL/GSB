"""Deterministic masked-denoising context scorer."""

from __future__ import annotations

import math
from collections import Counter, defaultdict
from dataclasses import dataclass
from typing import Any, Iterable

from .text import split_sentences, tokenize, words


BOS = "<s>"
EOS = "</s>"


@dataclass(frozen=True)
class PositionScore:
    word_ordinal: int
    sentence_index: int
    token: str
    masked_sentence: str
    probability: float
    log_probability: float
    rank: int
    restored_word: str
    restored_correctly: bool

    def as_dict(self) -> dict[str, Any]:
        return {
            "word_ordinal": self.word_ordinal,
            "sentence_index": self.sentence_index,
            "token": self.token,
            "masked_sentence": self.masked_sentence,
            "probability": round(self.probability, 9),
            "log_probability": round(self.log_probability, 9),
            "rank": self.rank,
            "restored_word": self.restored_word,
            "restored_correctly": self.restored_correctly,
        }


class MaskedDenoisingScorer:
    """A tiny interpolated n-gram model with explicit position buckets.

    Every word position is masked and restored from the remaining local
    context.  Counts are built only from the locally supplied corpus, so the
    model is deterministic and has no network or package dependency.
    """

    MODEL_VERSION = "masked-denoise-ngram-v1"

    def __init__(self, corpus: Iterable[str], lexicon: dict[str, dict[str, str]]):
        self.lexicon = lexicon
        self.unigram: Counter[str] = Counter()
        self.left_bigram: Counter[tuple[str, str]] = Counter()
        self.right_bigram: Counter[tuple[str, str]] = Counter()
        self.pair_context: Counter[tuple[str, str, str]] = Counter()
        self.position: Counter[tuple[str, str]] = Counter()
        self.left_total: Counter[str] = Counter()
        self.right_total: Counter[str] = Counter()
        self.pair_total: Counter[tuple[str, str]] = Counter()
        self.position_total: Counter[str] = Counter()
        self._ranking_cache: dict[tuple[str, str, str], tuple[list[tuple[str, float]], dict[str, int]]] = {}
        self.corpus_sentence_count = 0

        corpus_words: set[str] = set()
        for sentence in sorted(corpus):
            sequence = [word.lower() for word in words(sentence)]
            if not sequence:
                continue
            self.corpus_sentence_count += 1
            corpus_words.update(sequence)
            position_buckets = self._buckets(len(sequence))
            for index, word in enumerate(sequence):
                left = sequence[index - 1] if index else BOS
                right = sequence[index + 1] if index + 1 < len(sequence) else EOS
                bucket = position_buckets[index]
                self.unigram[word] += 1
                self.left_bigram[(left, word)] += 1
                self.right_bigram[(right, word)] += 1
                self.pair_context[(left, right, word)] += 1
                self.position[(bucket, word)] += 1
                self.left_total[left] += 1
                self.right_total[right] += 1
                self.pair_total[(left, right)] += 1
                self.position_total[bucket] += 1

        self.vocabulary = sorted(corpus_words | set(lexicon))
        self.vocabulary_size = max(1, len(self.vocabulary))
        self.total_words = max(1, sum(self.unigram.values()))

    @staticmethod
    def _buckets(length: int) -> list[str]:
        if length == 1:
            return ["only"]
        if length == 2:
            return ["start", "end"]
        buckets: list[str] = []
        for index in range(length):
            ratio = index / (length - 1)
            if index == 0:
                buckets.append("start")
            elif index == length - 1:
                buckets.append("end")
            elif ratio < 0.34:
                buckets.append("early")
            elif ratio > 0.66:
                buckets.append("late")
            else:
                buckets.append("middle")
        return buckets

    def _unigram_probability(self, word: str) -> float:
        return (self.unigram[word] + 0.05) / (self.total_words + 0.05 * self.vocabulary_size)

    def _probability(self, word: str, left: str, right: str, bucket: str) -> float:
        unigram = self._unigram_probability(word)
        left_probability = (
            self.left_bigram[(left, word)] + 0.35 * unigram
        ) / (self.left_total[left] + 0.35)
        right_probability = (
            self.right_bigram[(right, word)] + 0.35 * unigram
        ) / (self.right_total[right] + 0.35)
        pair_probability = (
            self.pair_context[(left, right, word)] + 0.45 * left_probability
        ) / (self.pair_total[(left, right)] + 0.45)
        position_probability = (
            self.position[(bucket, word)] + 0.35 * unigram
        ) / (self.position_total[bucket] + 0.35)
        return max(
            1e-15,
            0.35 * left_probability
            + 0.25 * right_probability
            + 0.20 * pair_probability
            + 0.12 * position_probability
            + 0.08 * unigram,
        )

    def _context_ranking(
        self, left: str, right: str, bucket: str
    ) -> tuple[list[tuple[str, float]], dict[str, int]]:
        key = (left, right, bucket)
        cached = self._ranking_cache.get(key)
        if cached is not None:
            return cached
        ranked = sorted(
            (
                (word, self._probability(word, left, right, bucket))
                for word in self.vocabulary
            ),
            key=lambda item: (-item[1], item[0]),
        )
        ranks = {word: index + 1 for index, (word, _) in enumerate(ranked)}
        self._ranking_cache[key] = (ranked, ranks)
        return ranked, ranks

    @staticmethod
    def _masked_sentence(sequence: list[str], mask_index: int) -> str:
        pieces = []
        for index, word in enumerate(sequence):
            pieces.append("[MASK]" if index == mask_index else word)
        return " ".join(pieces)

    def score_sentence(self, text: str) -> list[PositionScore]:
        scores: list[PositionScore] = []
        global_ordinal = 0
        for sentence_index, sentence in enumerate(split_sentences(text), start=1):
            sequence = [word.lower() for word in words(sentence)]
            if not sequence:
                continue
            buckets = self._buckets(len(sequence))
            for index, word in enumerate(sequence):
                global_ordinal += 1
                left = sequence[index - 1] if index else BOS
                right = sequence[index + 1] if index + 1 < len(sequence) else EOS
                bucket = buckets[index]
                ranked, ranks = self._context_ranking(left, right, bucket)
                probability = self._probability(word, left, right, bucket)
                scores.append(
                    PositionScore(
                        word_ordinal=global_ordinal,
                        sentence_index=sentence_index,
                        token=word,
                        masked_sentence=self._masked_sentence(sequence, index),
                        probability=probability,
                        log_probability=math.log(probability),
                        rank=ranks.get(word, self.vocabulary_size + 1),
                        restored_word=ranked[0][0] if ranked else "",
                        restored_correctly=bool(ranked and ranked[0][0] == word),
                    )
                )
        return scores

    def score_candidate(self, text: str) -> dict[str, Any]:
        scores = self.score_sentence(text)
        if not scores:
            return {
                "model_version": self.MODEL_VERSION,
                "average_log_probability": -1e9,
                "average_probability": 0.0,
                "perplexity": float("inf"),
                "mean_rank": float(self.vocabulary_size + 1),
                "restoration_accuracy": 0.0,
                "positions": [],
            }
        mean_log = sum(item.log_probability for item in scores) / len(scores)
        mean_probability = sum(item.probability for item in scores) / len(scores)
        return {
            "model_version": self.MODEL_VERSION,
            "average_log_probability": round(mean_log, 9),
            "average_probability": round(mean_probability, 9),
            "perplexity": round(math.exp(-mean_log), 6),
            "mean_rank": round(sum(item.rank for item in scores) / len(scores), 6),
            "restoration_accuracy": round(
                sum(1 for item in scores if item.restored_correctly) / len(scores), 9
            ),
            "positions": [item.as_dict() for item in scores],
        }

    def word_extra(self, word: str) -> dict[str, Any]:
        normalized = word.lower()
        return {
            "word": normalized,
            "pos": self.lexicon.get(normalized, {}).get("pos", "UNKNOWN"),
            "class": self.lexicon.get(normalized, {}).get("class", "unknown"),
            "unigram_count": self.unigram[normalized],
        }

    def describe(self) -> dict[str, Any]:
        return {
            "model_version": self.MODEL_VERSION,
            "training_sentences": self.corpus_sentence_count,
            "vocabulary_size": self.vocabulary_size,
            "total_tokens": self.total_words,
            "interpolation_weights": {
                "left_context": 0.35,
                "right_context": 0.25,
                "left_right_pair": 0.20,
                "position_bucket": 0.12,
                "unigram": 0.08,
            },
        }
