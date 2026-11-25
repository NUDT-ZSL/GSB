"""Cross-version overlap detection and deterministic adjudication."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Iterable

from .models import Utterance
from .results import LoadResult


@dataclass(frozen=True)
class CandidateScore:
    side: str
    utterance_id: str
    confidence: float
    revision: int
    lexical_rank: str

    def sort_key(self) -> tuple[float, int, str, int]:
        source_tie = 0 if self.side == "v1" else 1
        return (-self.confidence, -self.revision, self.lexical_rank, source_tie)

    def to_dict(self) -> dict[str, object]:
        return {
            "side": self.side,
            "utterance_id": self.utterance_id,
            "confidence": self.confidence,
            "revision": self.revision,
        }


@dataclass(frozen=True)
class AdjudicationDecision:
    conclusion: str
    winner_side: str | None
    winner_id: str | None
    priority_applied: str
    reason: str
    winner: Utterance | None = field(default=None, repr=False)
    supplements: dict[str, object] = field(default_factory=dict)

    def to_dict(self) -> dict[str, object]:
        labels = {"covered": "覆盖", "conflict": "冲突", "supplement": "补充"}
        return {
            "conclusion": self.conclusion,
            "conclusion_label_zh": labels.get(self.conclusion, self.conclusion),
            "winner_side": self.winner_side,
            "winner_id": self.winner_id,
            "priority_applied": self.priority_applied,
            "reason": self.reason,
            "supplements": self.supplements,
        }


@dataclass(frozen=True)
class AdjudicationPair:
    pair_key: str
    v1: Utterance | None
    v2: Utterance | None
    overlap_start_ms: int
    overlap_end_ms: int
    overlap_ms: int
    intersection_over_union: float
    overlap_to_shorter_ratio: float
    same_normalized_text: bool
    decision: AdjudicationDecision

    @property
    def v1_id(self) -> str | None:
        return self.v1.utterance_id if self.v1 else None

    @property
    def v2_id(self) -> str | None:
        return self.v2.utterance_id if self.v2 else None

    def to_dict(self, include_utterances: bool = False) -> dict[str, object]:
        data = {
            "pair_key": self.pair_key,
            "v1_id": self.v1_id,
            "v2_id": self.v2_id,
            "overlap_start_ms": self.overlap_start_ms,
            "overlap_end_ms": self.overlap_end_ms,
            "overlap_ms": self.overlap_ms,
            "intersection_over_union": round(self.intersection_over_union, 6),
            "overlap_to_shorter_ratio": round(self.overlap_to_shorter_ratio, 6),
            "same_normalized_text": self.same_normalized_text,
            "decision": self.decision.to_dict(),
        }
        if include_utterances:
            data["v1"] = self.v1.to_dict() if self.v1 else None
            data["v2"] = self.v2.to_dict() if self.v2 else None
        return data


@dataclass
class AdjudicationReport:
    pairs: list[AdjudicationPair] = field(default_factory=list)
    unmatched_v1: list[Utterance] = field(default_factory=list)
    unmatched_v2: list[Utterance] = field(default_factory=list)

    @property
    def conclusions(self) -> dict[str, int]:
        counts = {"covered": 0, "conflict": 0, "supplement": 0}
        for pair in self.pairs:
            counts[pair.decision.conclusion] = counts.get(pair.decision.conclusion, 0) + 1
        return counts

    def get(self, pair_key: str) -> AdjudicationPair | None:
        return next((pair for pair in self.pairs if pair.pair_key == pair_key), None)

    def to_dict(self) -> dict[str, object]:
        return {
            "pair_count": len(self.pairs),
            "conclusions": self.conclusions,
            "pairs": [pair.to_dict() for pair in self.pairs],
            "unmatched_v1": [item.utterance_id for item in self.unmatched_v1],
            "unmatched_v2": [item.utterance_id for item in self.unmatched_v2],
        }


def _items(source: LoadResult | Iterable[Utterance] | None) -> list[Utterance]:
    if source is None:
        return []
    if isinstance(source, LoadResult):
        return list(source.utterances)
    return list(source)


def _rank(item: Utterance, side: str) -> CandidateScore:
    return CandidateScore(
        side=side,
        utterance_id=item.utterance_id,
        confidence=item.confidence,
        revision=item.revision,
        lexical_rank=item.utterance_id,
    )


def choose_winner(v1: Utterance, v2: Utterance) -> tuple[Utterance, str, str]:
    """Apply confidence, revision, then lexicographic utterance_id priority."""

    first = _rank(v1, "v1")
    second = _rank(v2, "v2")
    if first.confidence != second.confidence:
        winner = v1 if first.confidence > second.confidence else v2
        return winner, "confidence", f"confidence {max(first.confidence, second.confidence):.3f} is higher"
    if first.revision != second.revision:
        winner = v1 if first.revision > second.revision else v2
        return winner, "revision", f"confidence tied; revision {max(first.revision, second.revision)} is higher"

    sorted_candidates = sorted([first, second], key=CandidateScore.sort_key)
    selected_score = sorted_candidates[0]
    winner = v1 if selected_score.side == "v1" else v2
    if first.utterance_id != second.utterance_id:
        reason = f"confidence and revision tied; lexicographically smaller id {selected_score.utterance_id!r} wins"
        applied = "utterance_id"
    else:
        reason = "confidence, revision and id are tied; deterministic source-order fallback selects v1"
        applied = "utterance_id_tie_source_order"
    return winner, applied, reason


def _classify_and_decide(
    v1: Utterance,
    v2: Utterance,
    *,
    iou: float,
    overlap_to_shorter: float,
    same_text: bool,
    exact_boundaries: bool,
) -> AdjudicationDecision:
    winner, priority, reason = choose_winner(v1, v2)
    winner_side = "v1" if winner is v1 else "v2"
    supplements: dict[str, object] = {}

    v2_enrichment = {
        "speaker": v2.speaker if v1.speaker is None and v2.speaker is not None else None,
        "word_count": len(v2.words) if not v1.words and v2.words else None,
        "revision": v2.revision if v2.revision > v1.revision else None,
    }
    v2_enrichment = {key: value for key, value in v2_enrichment.items() if value is not None}
    if v2_enrichment:
        supplements["v2_enrichment"] = v2_enrichment

    if same_text and (exact_boundaries or iou >= 0.85):
        return AdjudicationDecision(
            conclusion="covered",
            winner_side=winner_side,
            winner_id=winner.utterance_id,
            priority_applied="normalized_text_match",
            reason="normalized text matches and intervals cover the same audio range",
            winner=winner,
            supplements=supplements,
        )

    if not same_text and overlap_to_shorter >= 0.80 and (exact_boundaries or iou >= 0.70):
        return AdjudicationDecision(
            conclusion="conflict",
            winner_side=winner_side,
            winner_id=winner.utterance_id,
            priority_applied=priority,
            reason=reason,
            winner=winner,
            supplements=supplements,
        )

    supplements.update(
        {
            "partial_overlap": True,
            "overlap_to_shorter_ratio": round(overlap_to_shorter, 4),
            "text_conflict_in_overlap": not same_text,
        }
    )
    return AdjudicationDecision(
        conclusion="supplement",
        winner_side=winner_side,
        winner_id=winner.utterance_id,
        priority_applied=priority if not same_text else "complementary_interval",
        reason=(
            "partial overlap requires retaining complementary interval portions; "
            + (reason if not same_text else "matching text does not resolve boundary supplementation")
        ),
        winner=winner,
        supplements=supplements,
    )


def adjudicate(
    v1_source: LoadResult | Iterable[Utterance] | None,
    v2_source: LoadResult | Iterable[Utterance] | None,
) -> AdjudicationReport:
    v1_items = _items(v1_source)
    v2_items = _items(v2_source)
    pairs: list[AdjudicationPair] = []
    matched_v1: set[str] = set()
    matched_v2: set[str] = set()

    for v1 in v1_items:
        for v2 in v2_items:
            overlap_start = max(v1.start_ms, v2.start_ms)
            overlap_end = min(v1.end_ms, v2.end_ms)
            overlap_ms = max(0, overlap_end - overlap_start)
            if overlap_ms <= 0:
                continue

            union_ms = max(v1.end_ms, v2.end_ms) - min(v1.start_ms, v2.start_ms)
            iou = overlap_ms / union_ms if union_ms else 1.0
            shorter = min(v1.duration_ms, v2.duration_ms)
            overlap_to_shorter = overlap_ms / shorter if shorter else 1.0
            same_text = v1.normalized_text() == v2.normalized_text()
            exact_boundaries = v1.start_ms == v2.start_ms and v1.end_ms == v2.end_ms
            pair_key = f"{v1.utterance_id}__x__{v2.utterance_id}"
            decision = _classify_and_decide(
                v1,
                v2,
                iou=iou,
                overlap_to_shorter=overlap_to_shorter,
                same_text=same_text,
                exact_boundaries=exact_boundaries,
            )
            pairs.append(
                AdjudicationPair(
                    pair_key=pair_key,
                    v1=v1,
                    v2=v2,
                    overlap_start_ms=overlap_start,
                    overlap_end_ms=overlap_end,
                    overlap_ms=overlap_ms,
                    intersection_over_union=iou,
                    overlap_to_shorter_ratio=overlap_to_shorter,
                    same_normalized_text=same_text,
                    decision=decision,
                )
            )
            matched_v1.add(v1.utterance_id)
            matched_v2.add(v2.utterance_id)

    return AdjudicationReport(
        pairs=pairs,
        unmatched_v1=[item for item in v1_items if item.utterance_id not in matched_v1],
        unmatched_v2=[item for item in v2_items if item.utterance_id not in matched_v2],
    )
