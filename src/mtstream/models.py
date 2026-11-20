"""Immutable data models used across all workbench stages."""

from __future__ import annotations

import math
from dataclasses import dataclass, field, replace
from typing import Any, Iterable

from .util import DECLARED_PRECISION, qfloat, qtuple, sha256_value, stable_hash


@dataclass(frozen=True)
class Alignment:
    source_index: int
    target_index: int
    target_token: str
    weight: float = 1.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "source_index": self.source_index,
            "target_index": self.target_index,
            "target_token": self.target_token,
            "weight": qfloat(self.weight),
        }

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "Alignment":
        return cls(
            source_index=int(raw["source_index"]),
            target_index=int(raw["target_index"]),
            target_token=str(raw["target_token"]),
            weight=float(raw.get("weight", 1.0)),
        )


@dataclass(frozen=True)
class TranslationCase:
    case_id: str
    seed: int
    source_tokens: tuple[str, ...]
    source_vocab: tuple[str, ...]
    target_vocab: tuple[str, ...]
    pair_vocab: tuple[tuple[str, str], ...]
    alignments: tuple[Alignment, ...]
    source_version: int = 1
    align_version: int = 1

    @property
    def target_length(self) -> int:
        if not self.alignments:
            return 0
        return max(a.target_index for a in self.alignments) + 1

    @property
    def source_digest(self) -> str:
        return sha256_value(
            {
                "case_id": self.case_id,
                "seed": self.seed,
                "source_tokens": list(self.source_tokens),
                "source_version": self.source_version,
            }
        )

    @property
    def align_checksum(self) -> str:
        return sha256_value(
            {
                "align_version": self.align_version,
                "alignments": [a.to_dict() for a in self.alignments],
                "target_vocab": list(self.target_vocab),
            }
        )

    @property
    def pair_vocab_dicts(self) -> tuple[dict[str, str], ...]:
        return tuple({"source": s, "target": t} for s, t in self.pair_vocab)

    def aligned_source_indices(self) -> frozenset[int]:
        return frozenset(a.source_index for a in self.alignments)

    def alignments_by_target(self) -> dict[int, tuple[Alignment, ...]]:
        result: dict[int, list[Alignment]] = {}
        for alignment in self.alignments:
            result.setdefault(alignment.target_index, []).append(alignment)
        return {key: tuple(value) for key, value in sorted(result.items())}

    def with_source_token(self, index: int, token: str) -> "TranslationCase":
        tokens = list(self.source_tokens)
        tokens[index] = token
        pair_vocab = sorted(
            {
                (tokens[alignment.source_index], alignment.target_token)
                for alignment in self.alignments
            }
        )
        return replace(
            self,
            source_tokens=tuple(tokens),
            pair_vocab=tuple(pair_vocab),
            source_version=self.source_version + 1,
        )

    def with_align_version(self, version: int) -> "TranslationCase":
        return replace(self, align_version=int(version))

    def with_source_version(self, version: int) -> "TranslationCase":
        return replace(self, source_version=int(version))

    def to_dict(self) -> dict[str, Any]:
        return {
            "case_id": self.case_id,
            "seed": self.seed,
            "source_tokens": list(self.source_tokens),
            "source_vocab": list(self.source_vocab),
            "target_vocab": list(self.target_vocab),
            "pair_vocab": [
                {"source": source, "target": target}
                for source, target in self.pair_vocab
            ],
            "alignments": [a.to_dict() for a in self.alignments],
            "source_version": self.source_version,
            "align_version": self.align_version,
        }

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "TranslationCase":
        pair_vocab = tuple(
            (str(p["source"]), str(p["target"])) for p in raw.get("pair_vocab", ())
        )
        return cls(
            case_id=str(raw["case_id"]),
            seed=int(raw["seed"]),
            source_tokens=tuple(str(v) for v in raw["source_tokens"]),
            source_vocab=tuple(str(v) for v in raw["source_vocab"]),
            target_vocab=tuple(str(v) for v in raw["target_vocab"]),
            pair_vocab=pair_vocab,
            alignments=tuple(
                Alignment.from_dict(item) for item in raw.get("alignments", ())
            ),
            source_version=int(raw.get("source_version", 1)),
            align_version=int(raw.get("align_version", 1)),
        )


@dataclass(frozen=True)
class DecodeConfig:
    embedding_dim: int = 16
    context_window: int = 3
    block_size: int = 4
    latency_budget_ms: float = 250.0
    max_source_tokens: int = 64
    max_cache_entries: int = 4096
    max_block_memory_bytes: int = 2_000_000
    max_context_window: int = 64
    precision: int = DECLARED_PRECISION

    def to_dict(self) -> dict[str, Any]:
        return {
            "embedding_dim": self.embedding_dim,
            "context_window": self.context_window,
            "block_size": self.block_size,
            "latency_budget_ms": qfloat(self.latency_budget_ms),
            "max_source_tokens": self.max_source_tokens,
            "max_cache_entries": self.max_cache_entries,
            "max_block_memory_bytes": self.max_block_memory_bytes,
            "max_context_window": self.max_context_window,
            "precision": self.precision,
        }

    @property
    def checksum(self) -> str:
        return sha256_value(self.to_dict())

    def with_changes(self, **changes: Any) -> "DecodeConfig":
        return replace(self, **changes)


@dataclass(frozen=True)
class DecoderState:
    step: int
    tokens: tuple[str, ...]
    confidences: tuple[float, ...]
    projections: tuple[tuple[float, ...], ...]
    previous_projection: tuple[float, ...]
    previous_target_token: str | None
    normalizer_sum: float
    attention_norm_sum: float
    state_hash: str

    @classmethod
    def initial(cls, embedding_dim: int) -> "DecoderState":
        zero = tuple(0.0 for _ in range(embedding_dim))
        seed = cls(
            step=0,
            tokens=(),
            confidences=(),
            projections=(),
            previous_projection=zero,
            previous_target_token=None,
            normalizer_sum=0.0,
            attention_norm_sum=0.0,
            state_hash="",
        )
        return replace(seed, state_hash=seed.compute_hash())

    def semantic_dict(self) -> dict[str, Any]:
        return {
            "step": self.step,
            "tokens": list(self.tokens),
            "confidences": [qfloat(v) for v in self.confidences],
            "projections": [list(qtuple(p)) for p in self.projections],
            "previous_projection": list(qtuple(self.previous_projection)),
            "previous_target_token": self.previous_target_token,
            "normalizer_sum": qfloat(self.normalizer_sum),
            "attention_norm_sum": qfloat(self.attention_norm_sum),
        }

    def compute_hash(self) -> str:
        return stable_hash("decoder-state-v1", self.semantic_dict())

    def to_dict(self) -> dict[str, Any]:
        payload = self.semantic_dict()
        payload["state_hash"] = self.state_hash
        return payload

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "DecoderState":
        state = cls(
            step=int(raw["step"]),
            tokens=tuple(str(v) for v in raw["tokens"]),
            confidences=tuple(float(v) for v in raw["confidences"]),
            projections=tuple(
                tuple(float(v) for v in projection)
                for projection in raw["projections"]
            ),
            previous_projection=tuple(
                float(v) for v in raw["previous_projection"]
            ),
            previous_target_token=raw.get("previous_target_token"),
            normalizer_sum=float(raw["normalizer_sum"]),
            attention_norm_sum=float(raw["attention_norm_sum"]),
            state_hash=str(raw.get("state_hash", "")),
        )
        expected = state.compute_hash()
        return replace(state, state_hash=expected)


@dataclass(frozen=True)
class CandidateRecord:
    token: str
    score: float
    probability: float
    rank: int
    has_current_alignment: bool
    aligned_source_indices: tuple[int, ...]
    attention_weights: tuple[float, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "token": self.token,
            "score": qfloat(self.score),
            "probability": qfloat(self.probability),
            "rank": self.rank,
            "has_current_alignment": self.has_current_alignment,
            "aligned_source_indices": list(self.aligned_source_indices),
            "attention_weights": [qfloat(v) for v in self.attention_weights],
        }

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "CandidateRecord":
        return cls(
            token=str(raw["token"]),
            score=float(raw["score"]),
            probability=float(raw["probability"]),
            rank=int(raw["rank"]),
            has_current_alignment=bool(raw["has_current_alignment"]),
            aligned_source_indices=tuple(
                int(value) for value in raw["aligned_source_indices"]
            ),
            attention_weights=tuple(
                float(value) for value in raw["attention_weights"]
            ),
        )


@dataclass(frozen=True)
class StepRecord:
    step: int
    block_index: int
    token: str
    confidence: float
    projection: tuple[float, ...]
    candidates: tuple[CandidateRecord, ...]
    candidate_log_normalizer: float
    attention_log_normalizer: float
    context_source_indices: tuple[int, ...]
    state_hash: str
    input_version: dict[str, Any]

    def to_dict(self, include_candidates: bool = True) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "step": self.step,
            "block_index": self.block_index,
            "token": self.token,
            "confidence": qfloat(self.confidence),
            "projection": [qfloat(v) for v in self.projection],
            "candidate_log_normalizer": qfloat(self.candidate_log_normalizer),
            "attention_log_normalizer": qfloat(self.attention_log_normalizer),
            "context_source_indices": list(self.context_source_indices),
            "state_hash": self.state_hash,
            "input_version": self.input_version,
        }
        if include_candidates:
            payload["candidates"] = [c.to_dict() for c in self.candidates]
        return payload

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "StepRecord":
        return cls(
            step=int(raw["step"]),
            block_index=int(raw["block_index"]),
            token=str(raw["token"]),
            confidence=float(raw["confidence"]),
            projection=tuple(
                float(value) for value in raw["projection"]
            ),
            candidates=tuple(
                CandidateRecord.from_dict(candidate)
                for candidate in raw.get("candidates", ())
            ),
            candidate_log_normalizer=float(
                raw["candidate_log_normalizer"]
            ),
            attention_log_normalizer=float(
                raw["attention_log_normalizer"]
            ),
            context_source_indices=tuple(
                int(value) for value in raw["context_source_indices"]
            ),
            state_hash=str(raw["state_hash"]),
            input_version=dict(raw["input_version"]),
        )


@dataclass(frozen=True)
class TimingRecord:
    stage: str
    observed_ns: int
    estimated_ns: int
    step_count: int
    candidate_score_count: int
    cache_entry_count: int
    budget_ns: int | None = None
    within_budget: bool | None = None

    @property
    def observed_ms(self) -> float:
        return qfloat(self.observed_ns / 1_000_000.0)

    @property
    def estimated_ms(self) -> float:
        return qfloat(self.estimated_ns / 1_000_000.0)

    def to_dict(self) -> dict[str, Any]:
        return {
            "stage": self.stage,
            "observed_ns": self.observed_ns,
            "observed_ms": self.observed_ms,
            "estimated_ns": self.estimated_ns,
            "estimated_ms": self.estimated_ms,
            "step_count": self.step_count,
            "candidate_score_count": self.candidate_score_count,
            "cache_entry_count": self.cache_entry_count,
            "budget_ns": self.budget_ns,
            "within_budget": self.within_budget,
        }


@dataclass(frozen=True)
class FullDecodeResult:
    case_id: str
    source_version: int
    align_version: int
    source_digest: str
    encoding_digest: str
    tokens: tuple[str, ...]
    confidences: tuple[float, ...]
    projections: tuple[tuple[float, ...], ...]
    step_records: tuple[StepRecord, ...]
    states: tuple[DecoderState, ...]
    timing: TimingRecord
    semantic_hash: str
    intermediate_digest: str

    def to_dict(self, include_candidates: bool = False) -> dict[str, Any]:
        return {
            "case_id": self.case_id,
            "source_version": self.source_version,
            "align_version": self.align_version,
            "source_digest": self.source_digest,
            "encoding_digest": self.encoding_digest,
            "tokens": list(self.tokens),
            "confidences": [qfloat(v) for v in self.confidences],
            "projections": [list(qtuple(v)) for v in self.projections],
            "steps": [
                record.to_dict(include_candidates=include_candidates)
                for record in self.step_records
            ],
            "timing": self.timing.to_dict(),
            "semantic_hash": self.semantic_hash,
            "intermediate_digest": self.intermediate_digest,
        }

    def semantic_dict(
        self, include_candidates: bool = False
    ) -> dict[str, Any]:
        payload = self.to_dict(include_candidates=include_candidates)
        payload.pop("timing", None)
        return payload


@dataclass(frozen=True)
class BlockPlan:
    plan_id: str
    case_id: str
    source_version: int
    align_version: int
    source_encoding_digest: str
    block_size_requested: int
    block_size_effective: int
    context_window: int
    boundaries: tuple[tuple[int, int], ...]
    snapshot_versions: dict[str, int]
    snapshot_checksums: dict[str, str]
    degraded: bool
    degradation_category: str | None
    context: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "plan_id": self.plan_id,
            "case_id": self.case_id,
            "source_version": self.source_version,
            "align_version": self.align_version,
            "source_encoding_digest": self.source_encoding_digest,
            "block_size_requested": self.block_size_requested,
            "block_size_effective": self.block_size_effective,
            "context_window": self.context_window,
            "boundaries": [
                {"block_index": i, "start_step": start, "end_step_exclusive": end}
                for i, (start, end) in enumerate(self.boundaries)
            ],
            "snapshot_versions": dict(sorted(self.snapshot_versions.items())),
            "snapshot_checksums": dict(sorted(self.snapshot_checksums.items())),
            "degraded": self.degraded,
            "degradation_category": self.degradation_category,
        }


@dataclass(frozen=True)
class BlockResult:
    block_index: int
    plan_id: str
    start_step: int
    end_step_exclusive: int
    tokens: tuple[str, ...]
    confidences: tuple[float, ...]
    projections: tuple[tuple[float, ...], ...]
    step_records: tuple[StepRecord, ...]
    source_version: int
    align_version: int
    cache_entry_keys: tuple[str, ...]
    timing: TimingRecord
    semantic_hash: str
    degraded: bool = False
    degradation_category: str | None = None

    def to_dict(self, include_candidates: bool = False) -> dict[str, Any]:
        return {
            "block_index": self.block_index,
            "plan_id": self.plan_id,
            "start_step": self.start_step,
            "end_step_exclusive": self.end_step_exclusive,
            "tokens": list(self.tokens),
            "confidences": [qfloat(v) for v in self.confidences],
            "projections": [list(qtuple(v)) for v in self.projections],
            "steps": [
                record.to_dict(include_candidates=include_candidates)
                for record in self.step_records
            ],
            "source_version": self.source_version,
            "align_version": self.align_version,
            "cache_entry_keys": list(self.cache_entry_keys),
            "timing": self.timing.to_dict(),
            "semantic_hash": self.semantic_hash,
            "degraded": self.degraded,
            "degradation_category": self.degradation_category,
        }

    def semantic_dict(
        self, include_candidates: bool = False
    ) -> dict[str, Any]:
        payload = self.to_dict(include_candidates=include_candidates)
        payload.pop("timing", None)
        return payload


@dataclass(frozen=True)
class InvalidationReport:
    reason: str
    first_affected_step: int | None
    affected_steps: tuple[int, ...]
    reused_steps: tuple[int, ...]
    affected_cache_entries: tuple[str, ...]
    reused_cache_entries: tuple[str, ...]
    old_source_digest: str
    new_source_digest: str
    old_align_version: int
    new_align_version: int
    old_block_size: int
    new_block_size: int
    details: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        affected = set(self.affected_steps)
        reused = set(self.reused_steps)
        covered = affected | reused
        expected_range = set(
            range(
                0,
                max(covered) + 1 if covered else 0,
            )
        )
        return {
            "reason": self.reason,
            "first_affected_step": self.first_affected_step,
            "affected_steps": list(self.affected_steps),
            "reused_steps": list(self.reused_steps),
            "affected_cache_entries": list(self.affected_cache_entries),
            "reused_cache_entries": list(self.reused_cache_entries),
            "old_source_digest": self.old_source_digest,
            "new_source_digest": self.new_source_digest,
            "old_align_version": self.old_align_version,
            "new_align_version": self.new_align_version,
            "old_block_size": self.old_block_size,
            "new_block_size": self.new_block_size,
            "details": self.details,
            "set_check": {
                "disjoint": not bool(affected & reused),
                "covers_all_steps": covered == expected_range,
            },
        }


def semantic_step_payload(records: Iterable[StepRecord]) -> dict[str, Any]:
    return {
        "steps": [record.to_dict(include_candidates=True) for record in records],
    }


def finite_float(value: float) -> bool:
    return isinstance(value, (int, float)) and math.isfinite(float(value))

