"""Deterministic multi-channel recall.

Three channels are implemented:

* ``lexical``      - cosine overlap between user history terms and item terms,
* ``collaborative``- item-item cosine similarity from binary co-occurrence,
* ``popularity``   - time-decayed interaction popularity.

No randomness is used anywhere. Ties always resolve by item ID lexicographic
order so identical inputs always produce identical candidate lists.
"""

from __future__ import annotations

import math
import os
from dataclasses import dataclass
from typing import Any

import numpy as np

from .parser import ParsedData


@dataclass
class Candidate:
    item_id: str
    score: float

    def to_dict(self) -> dict[str, Any]:
        return {"item_id": self.item_id, "score": round(float(self.score), 10)}

    @staticmethod
    def from_dict(payload: dict[str, Any]) -> "Candidate":
        return Candidate(payload["item_id"], float(payload["score"]))


def _maybe_fault(stage: str, user_id: str) -> None:
    """Test hook: RECALL_BENCH_FAULT=user_id:stage raises inside that user."""
    spec = os.environ.get("RECALL_BENCH_FAULT")
    if not spec or ":" not in spec:
        return
    target_user, target_stage = spec.split(":", 1)
    if target_user == user_id and target_stage == stage:
        raise RuntimeError(f"injected fault at stage={stage} user={user_id}")


class ChannelEngine:
    def __init__(self, data: ParsedData, config: dict[str, Any]):
        self.data = data
        self.config = config
        self.item_ids = sorted(data.item_terms.keys())
        self.item_index = {item_id: idx for idx, item_id in enumerate(self.item_ids)}
        vocab = sorted({term for terms in data.item_terms.values() for term in terms})
        self.vocab_index = {term: idx for idx, term in enumerate(vocab)}
        self._item_term_matrix = self._build_item_term_matrix()
        self._item_similarity = self._build_item_similarity()
        self._popularity_scores = self._build_popularity()

    def _build_item_term_matrix(self) -> np.ndarray:
        matrix = np.zeros((len(self.item_ids), len(self.vocab_index)), dtype=np.float64)
        for item_id, terms in self.data.item_terms.items():
            row = self.item_index[item_id]
            for term in terms:
                matrix[row, self.vocab_index[term]] += 1.0
        return matrix

    def _build_item_similarity(self) -> np.ndarray:
        n_items = len(self.item_ids)
        user_item = np.zeros((max(self.data.user_count, 1), n_items), dtype=np.float64)
        user_order = sorted(self.data.user_interactions.keys())
        user_row = {user_id: idx for idx, user_id in enumerate(user_order)}
        for user_id, item_list in self.data.user_items.items():
            row = user_row[user_id]
            for item_id in item_list:
                matrix_row = self.item_index.get(item_id)
                if matrix_row is not None:
                    user_item[row, matrix_row] = 1.0
        norms = np.sqrt((user_item * user_item).sum(axis=0))
        with np.errstate(invalid="ignore", divide="ignore"):
            normalized = np.where(norms > 0, user_item / np.where(norms > 0, norms, 1.0), 0.0)
        similarity = normalized.T @ normalized
        np.fill_diagonal(similarity, 0.0)
        return np.nan_to_num(similarity)

    def _build_popularity(self) -> dict[str, float]:
        if not self.data.interactions:
            return {}
        half_life = float(self.config["runtime"]["popularity_half_life_days"]) * 86400.0
        decay_constant = math.log(2.0) / half_life
        reference = max(row["ts_epoch"] for row in self.data.interactions)
        scores = {item_id: 0.0 for item_id in self.item_ids}
        for row in self.data.interactions:
            scores[row["item_id"]] += math.exp(-decay_constant * (reference - row["ts_epoch"]))
        return scores

    def run_channel(self, channel: str, user_id: str) -> tuple[list[Candidate], str | None]:
        if not self.config["channels"][channel]["enabled"]:
            return [], "channel_disabled"
        if channel == "lexical":
            return self._lexical(user_id)
        if channel == "collaborative":
            return self._collaborative(user_id)
        if channel == "popularity":
            return self._popularity(user_id)
        raise ValueError(f"unknown channel {channel}")

    def _candidate_pool(self, user_id: str) -> list[str]:
        seen = set(self.data.user_items.get(user_id, []))
        return [item_id for item_id in self.item_ids if item_id not in seen]

    def _topn(self, scores: dict[str, float], limit: int) -> list[Candidate]:
        ordered = sorted(scores.items(), key=lambda pair: (-pair[1], pair[0]))
        return [Candidate(item_id, score) for item_id, score in ordered[:limit] if score > 0.0]

    def _lexical(self, user_id: str) -> tuple[list[Candidate], str | None]:
        _maybe_fault("recall_lexical", user_id)
        history = self.data.user_items.get(user_id)
        if not history:
            return [], "no_user_history"
        profile = np.zeros(len(self.vocab_index), dtype=np.float64)
        for item_id in history:
            profile += self._item_term_matrix[self.item_index[item_id]]
        norm = np.linalg.norm(profile)
        if norm == 0.0:
            return [], "empty_user_terms"
        profile = profile / norm
        item_norms = np.linalg.norm(self._item_term_matrix, axis=1)
        with np.errstate(invalid="ignore", divide="ignore"):
            unit_items = np.where(
                item_norms[:, None] > 0,
                self._item_term_matrix / np.where(item_norms[:, None] > 0, item_norms[:, None], 1.0),
                0.0,
            )
        raw_scores = unit_items @ profile
        scores: dict[str, float] = {}
        for item_id in self._candidate_pool(user_id):
            value = float(raw_scores[self.item_index[item_id]])
            if value > 0.0:
                scores[item_id] = value
        return self._topn(scores, self.config["channel_limits"]["lexical"]), None

    def _collaborative(self, user_id: str) -> tuple[list[Candidate], str | None]:
        _maybe_fault("recall_collaborative", user_id)
        history = self.data.user_items.get(user_id)
        if not history:
            return [], "no_user_history"
        if self.data.interaction_count < 2 or len(self.item_ids) < 2:
            return [], "insufficient_cooccurrence"
        history_rows = [self.item_index[item_id] for item_id in history if item_id in self.item_index]
        if not history_rows:
            return [], "insufficient_cooccurrence"
        aggregate = self._item_similarity[history_rows].sum(axis=0)
        scores: dict[str, float] = {}
        for item_id in self._candidate_pool(user_id):
            value = float(aggregate[self.item_index[item_id]])
            if value > 0.0:
                scores[item_id] = value
        return self._topn(scores, self.config["channel_limits"]["collaborative"]), None

    def _popularity(self, user_id: str) -> tuple[list[Candidate], str | None]:
        _maybe_fault("recall_popularity", user_id)
        if not self._popularity_scores:
            return [], "no_interactions"
        scores = {
            item_id: self._popularity_scores[item_id]
            for item_id in self._candidate_pool(user_id)
            if self._popularity_scores.get(item_id, 0.0) > 0.0
        }
        if not scores:
            return [], "all_items_consumed"
        return self._topn(scores, self.config["channel_limits"]["popularity"]), None

    def global_stats(self) -> dict[str, Any]:
        return {
            "popularity_reference_max": round(
                max((row["ts_epoch"] for row in self.data.interactions), default=0.0), 6
            ),
            "popularity_top5": [
                {"item_id": item_id, "score": round(score, 10)}
                for item_id, score in sorted(
                    self._popularity_scores.items(), key=lambda pair: (-pair[1], pair[0])
                )[:5]
            ],
            "cooccurrence_nonzero": int(np.count_nonzero(self._item_similarity)),
        }
