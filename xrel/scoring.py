"""Bridge-confidence weighted BM25 scoring across language partitions."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List

from .config import AppConfig, MERGE_MAX, MERGE_SUM
from .errors import ResourceLimitExceeded
from .expansion import ExpansionResult
from .index import InvertedIndex, PartitionStats
from .numerics import NumericBackend


@dataclass
class Contribution:
    """One candidate -> one document contribution, fully replayable."""

    target_lang: str
    target_term: str
    doc_id: str
    tf: int
    df: int
    raw_score: float          # backend value rendered as a JSON number
    weight: float
    weighted_contribution: float
    backend_raw: Any = field(repr=False, default=None)
    backend_weight: Any = field(repr=False, default=None)
    backend_contribution: Any = field(repr=False, default=None)

    def to_dict(self) -> dict:
        return {
            "target_lang": self.target_lang,
            "target_term": self.target_term,
            "doc_id": self.doc_id,
            "tf": self.tf,
            "df": self.df,
            "raw_score": self.raw_score,
            "weight": self.weight,
            "weighted_contribution": self.weighted_contribution,
        }


@dataclass
class DocScore:
    doc_id: str
    doc_lang: str
    score: float
    backend_score: Any = field(repr=False, default=None)
    contributions: List[Contribution] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "doc_id": self.doc_id,
            "doc_lang": self.doc_lang,
            "score": self.score,
            "contributions": [item.to_dict() for item in self.contributions],
        }


@dataclass
class BudgetCounters:
    posting_reads: int = 0
    candidate_comparisons: int = 0
    resident_detail_bytes: int = 0
    degraded: bool = False
    degrade_reasons: List[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "posting_reads": self.posting_reads,
            "candidate_comparisons": self.candidate_comparisons,
            "resident_detail_bytes": self.resident_detail_bytes,
            "degraded": self.degraded,
            "degrade_reasons": list(self.degrade_reasons),
        }


class ScoreEngine:
    """Runs the whole scoring graph through one numeric backend."""

    def __init__(self, backend: NumericBackend, config: AppConfig) -> None:
        config.validate()
        self.backend = backend
        self.config = config
        self.counters = BudgetCounters()
        self._constants = self._build_constants(backend, config)

    @staticmethod
    def _build_constants(backend: NumericBackend, config: AppConfig) -> Dict[str, Any]:
        k1 = backend.quantize(config.bm25.k1)
        b = backend.quantize(config.bm25.b)
        one = backend.quantize(1.0)
        # idf = log(1 + N/n), always non-negative.
        return {
            "k1": k1,
            "b": b,
            "one": one,
            "k1_plus_one": backend.add(k1, one),
            "one_minus_b": backend.sub(one, b),
        }

    # ------------------------------------------------------------------
    def _idf(self, partition: PartitionStats, df: int) -> Any:
        backend = self.backend
        n = backend.quantize(df)
        total = backend.quantize(partition.doc_count)
        ratio = backend.div(total, n)
        return backend.log(backend.add(self._constants["one"], ratio))

    def _bm25_term_doc(
        self, partition: PartitionStats, tf: int, df: int, doc: "DocLengthView"
    ) -> Any:
        backend = self.backend
        constants = self._constants
        idf = self._idf(partition, df)
        tf_q = backend.quantize(tf)
        # Both operands are scaled ints; div returns the correctly scaled ratio.
        norm = backend.div(backend.quantize(doc.doc_len), backend.quantize(partition.avg_len))
        # Equivalent explicit form: k1 * (1 - b + b * dl/avgdl)
        length_term = backend.add(
            constants["one_minus_b"], backend.mul(constants["b"], norm)
        )
        denominator = backend.add(tf_q, backend.mul(constants["k1"], length_term))
        numerator = backend.mul(tf_q, constants["k1_plus_one"])
        tf_saturation = backend.div(numerator, denominator)
        return backend.mul(idf, tf_saturation)

    # ------------------------------------------------------------------
    def score(
        self,
        expansion: ExpansionResult,
        index: InvertedIndex,
        doc_lengths: Dict[str, "DocLengthView"],
        streaming: bool = False,
    ) -> Dict[str, DocScore]:
        backend = self.backend
        merged: Dict[str, DocScore] = {}
        merge_rule = self.config.bm25.merge_rule

        for candidate in expansion.candidates:
            partition = index.partitions.get(candidate.target_lang)
            if partition is None:
                continue
            postings, df = self._postings_with_budget(partition, candidate.target_term)
            if not postings:
                continue
            weight = backend.quantize(candidate.confidence)
            for posting in postings:
                self.counters.candidate_comparisons += 1
                doc_view = doc_lengths[posting.doc_id]
                raw = self._bm25_term_doc(partition, posting.tf, df, doc_view)
                contribution_value = backend.mul(raw, weight)
                record = Contribution(
                    target_lang=candidate.target_lang,
                    target_term=candidate.target_term,
                    doc_id=posting.doc_id,
                    tf=posting.tf,
                    df=df,
                    raw_score=backend.to_float(raw),
                    weight=backend.to_float(weight),
                    weighted_contribution=backend.to_float(contribution_value),
                    backend_raw=raw,
                    backend_weight=weight,
                    backend_contribution=contribution_value,
                )
                self._account_detail(record, streaming)
                existing = merged.get(posting.doc_id)
                if existing is None:
                    merged[posting.doc_id] = DocScore(
                        doc_id=posting.doc_id,
                        doc_lang=candidate.target_lang,
                        score=backend.to_float(contribution_value),
                        backend_score=contribution_value,
                        contributions=[record],
                    )
                else:
                    existing.contributions.append(record)
                    if merge_rule == MERGE_SUM:
                        existing.backend_score = backend.add(
                            existing.backend_score, contribution_value
                        )
                    elif merge_rule == MERGE_MAX:
                        existing.backend_score = backend.maximum(
                            existing.backend_score, contribution_value
                        )
                    else:  # validated at config level
                        raise ValueError(merge_rule)
                    existing.score = backend.to_float(existing.backend_score)

        # Deterministic final summation order for SUM merges is already the
        # candidate traversal order; re-render scores once for stability.
        for doc_score in merged.values():
            if merge_rule == MERGE_SUM:
                ordered = [item.backend_contribution for item in doc_score.contributions]
                total = backend.sum_sorted(ordered)
                doc_score.backend_score = total
                doc_score.score = backend.to_float(total)
            doc_score.contributions.sort(
                key=lambda item: (item.target_lang, item.target_term, item.doc_id)
            )
        return merged

    # ------------------------------------------------------------------
    def _postings_with_budget(self, partition: PartitionStats, term: str):
        postings = partition.postings.get(term, [])
        self.counters.posting_reads += len(postings)
        limits = self.config.limits
        if self.counters.posting_reads > limits.max_posting_reads:
            if limits.allow_streaming_degrade:
                self._degrade("posting_read_streaming")
            else:
                raise ResourceLimitExceeded(
                    "max_posting_reads",
                    self.counters.posting_reads,
                    limits.max_posting_reads,
                )
        if self.counters.candidate_comparisons > limits.max_candidate_comparisons:
            if limits.allow_streaming_degrade:
                self._degrade("candidate_comparison_streaming")
            else:
                raise ResourceLimitExceeded(
                    "max_candidate_comparisons",
                    self.counters.candidate_comparisons,
                    limits.max_candidate_comparisons,
                )
        return postings, partition.df.get(term, 0)

    def _account_detail(self, record: Contribution, streaming: bool) -> None:
        if streaming:
            # Streaming mode keeps only lightweight records.
            self.counters.resident_detail_bytes += 64
            return
        size = len(record.to_dict().__repr__().encode("utf-8"))
        self.counters.resident_detail_bytes += size
        limits = self.config.limits
        if self.counters.resident_detail_bytes > limits.max_resident_detail_bytes:
            if limits.allow_streaming_degrade:
                self._degrade("resident_detail_streaming")
            else:
                raise ResourceLimitExceeded(
                    "max_resident_detail_bytes",
                    self.counters.resident_detail_bytes,
                    limits.max_resident_detail_bytes,
                )

    def _degrade(self, reason: str) -> None:
        self.counters.degraded = True
        if reason not in self.counters.degrade_reasons:
            self.counters.degrade_reasons.append(reason)


@dataclass(frozen=True)
class DocLengthView:
    doc_id: str
    doc_len: int


def build_doc_lengths(documents) -> Dict[str, DocLengthView]:
    return {
        doc.doc_id: DocLengthView(doc_id=doc.doc_id, doc_len=doc.doc_len)
        for doc in documents
    }


def replay_score(
    contributions: List[Contribution],
    backend: NumericBackend,
    merge_rule: str,
) -> float:
    """Independently recompute a document score from its contribution detail.

    Used by the replay/lineage checks.  Contributions are re-sorted so that
    summation order never depends on insertion history.
    """

    ordered = sorted(
        contributions,
        key=lambda item: (item.target_lang, item.target_term, item.doc_id),
    )
    if merge_rule == MERGE_SUM:
        total = backend.sum_sorted(
            backend.mul(backend.quantize(item.raw_score), backend.quantize(item.weight))
            for item in ordered
        )
        return backend.to_float(total)
    if merge_rule == MERGE_MAX:
        best = None
        for item in ordered:
            value = backend.mul(
                backend.quantize(item.raw_score), backend.quantize(item.weight)
            )
            best = value if best is None else backend.maximum(best, value)
        return backend.to_float(best if best is not None else backend.quantize(0.0))
    raise ValueError(merge_rule)
