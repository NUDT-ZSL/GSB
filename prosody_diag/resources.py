from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class ResourceGovernor:
    max_utterances_per_run: int
    max_resident_frames: int
    max_cache_entries: int
    default_prune_threshold: float
    prune_boost: float
    degradations: list[dict[str, Any]] = field(default_factory=list)
    streaming_features: bool = False
    candidate_score_threshold: float = 0.0

    def __post_init__(self) -> None:
        self.candidate_score_threshold = self.default_prune_threshold

    def batch_size(self, utterance_count: int) -> int:
        if utterance_count > self.max_utterances_per_run:
            self.degradations.append(
                {
                    "category": "batch_streaming",
                    "reason": "utterance_count_exceeds_max_utterances_per_run",
                    "utterance_count": utterance_count,
                    "limit": self.max_utterances_per_run,
                    "batch_size": self.max_utterances_per_run,
                }
            )
            return max(1, self.max_utterances_per_run)
        return utterance_count

    def observe_resident_frames(self, frame_count: int, utterance_id: str) -> None:
        if frame_count > self.max_resident_frames and not self.streaming_features:
            self.streaming_features = True
            self.degradations.append(
                {
                    "category": "feature_streaming",
                    "reason": "resident_feature_frames_exceed_limit",
                    "utterance_id": utterance_id,
                    "frame_count": frame_count,
                    "limit": self.max_resident_frames,
                    "action": "evict_frame_arrays_after_cache_and_retain_summaries",
                }
            )

    def observe_cache_entries(self, entry_count: int) -> None:
        if entry_count > self.max_cache_entries:
            new_threshold = round(min(0.95, self.candidate_score_threshold + self.prune_boost), 3)
            if new_threshold > self.candidate_score_threshold:
                self.candidate_score_threshold = new_threshold
                self.degradations.append(
                    {
                        "category": "cache_prune_threshold",
                        "reason": "cache_entry_count_exceeds_limit",
                        "entry_count": entry_count,
                        "limit": self.max_cache_entries,
                        "candidate_score_threshold": self.candidate_score_threshold,
                    }
                )

    def prune_candidates(self, candidates: list[dict[str, Any]]) -> list[dict[str, Any]]:
        if self.candidate_score_threshold <= self.default_prune_threshold:
            return candidates
        return [
            candidate
            for candidate in candidates
            if float(candidate["score"]) >= self.candidate_score_threshold
        ]
