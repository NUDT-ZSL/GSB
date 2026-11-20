"""Deterministic source encoder, per-step decoder and consistency checks."""

from __future__ import annotations

import hashlib
import math
import time
from dataclasses import dataclass, replace
from typing import Any, Sequence

import numpy as np

from .errors import E_CHUNK_MISMATCH, fail
from .models import (
    Alignment,
    CandidateRecord,
    DecodeConfig,
    DecoderState,
    FullDecodeResult,
    StepRecord,
    TimingRecord,
    TranslationCase,
    semantic_step_payload,
)
from .util import (
    canonical_json,
    logsumexp,
    qfloat,
    qtuple,
    sha256_value,
    stable_hash,
)

_ESTIMATE_FIXED_NS = 80_000
_ESTIMATE_STEP_NS = 22_000
_ESTIMATE_CANDIDATE_NS = 150
_ESTIMATE_CACHE_ENTRY_NS = 900


def semantic_config_checksum(config: DecodeConfig) -> str:
    """Hash only parameters that can change decoded values."""

    return stable_hash(
        "decoder-semantic-config-v1",
        {
            "embedding_dim": config.embedding_dim,
            "context_window": config.context_window,
            "precision": config.precision,
        },
    )


def _hash_unit_interval(seed: int, namespace: str, token: str, index: int) -> float:
    material = f"{seed}|{namespace}|{token}|{index}".encode("utf-8")
    digest = hashlib.sha256(material).digest()
    value = int.from_bytes(digest[:8], "big") / float(2**64 - 1)
    return value * 2.0 - 1.0


def deterministic_embedding(
    seed: int, namespace: str, token: str, dimension: int
) -> tuple[float, ...]:
    vector = np.asarray(
        [
            _hash_unit_interval(seed, namespace, token, index)
            for index in range(dimension)
        ],
        dtype=np.float64,
    )
    norm = float(np.linalg.norm(vector))
    if norm == 0.0:
        vector = np.zeros(dimension, dtype=np.float64)
        vector[0] = 1.0
    else:
        vector = vector / norm
    return qtuple(float(v) for v in vector)


@dataclass(frozen=True)
class EncodedSource:
    vectors: tuple[tuple[float, ...], ...]
    position_weights: tuple[float, ...]
    pooled: tuple[float, ...]
    encoding_digest: str


def encode_source(case: TranslationCase, config: DecodeConfig) -> EncodedSource:
    vectors = tuple(
        deterministic_embedding(
            case.seed, "source", token, config.embedding_dim
        )
        for token in case.source_tokens
    )
    weights = tuple(
        1.0 / math.log2(position + 2.0)
        for position in range(len(case.source_tokens))
    )
    weighted = np.zeros(config.embedding_dim, dtype=np.float64)
    weight_sum = 0.0
    for vector, weight in zip(vectors, weights):
        weighted += np.asarray(vector, dtype=np.float64) * weight
        weight_sum += weight
    pooled = weighted / weight_sum if weight_sum else weighted
    pooled_tuple = qtuple(float(v) for v in pooled)
    digest = sha256_value(
        {
            "seed": case.seed,
            "source_tokens": list(case.source_tokens),
            "position_weights": [qfloat(w) for w in weights],
            "vectors": [list(vector) for vector in vectors],
            "pooled": list(pooled_tuple),
        }
    )
    return EncodedSource(
        vectors=vectors,
        position_weights=weights,
        pooled=pooled_tuple,
        encoding_digest=digest,
    )


def source_prefix_encoding_digest(
    case: TranslationCase,
    encoded: EncodedSource,
    upto_step: int,
) -> str:
    """Digest of source encodings that a decoded prefix actually depends on.

    Unreferenced source words are intentionally excluded.  The cache key still
    contains a source encoding digest, but it is prefix-scoped to the aligned
    positions consumed by steps ``[0, upto_step)``.  This is what permits an
    unaffected prefix to be reused after a local source edit.
    """

    dependencies: dict[int, list[int]] = {}
    for alignment in case.alignments:
        if alignment.target_index < upto_step:
            dependencies.setdefault(alignment.source_index, []).append(
                alignment.target_index
            )
    payload = []
    for source_index in sorted(dependencies):
        payload.append(
            {
                "source_index": source_index,
                "source_token": case.source_tokens[source_index],
                "target_indices": sorted(set(dependencies[source_index])),
                "position_weight": qfloat(
                    encoded.position_weights[source_index]
                ),
                "encoding": list(encoded.vectors[source_index]),
            }
        )
    return sha256_value(
        {
            "case_id": case.case_id,
            "seed": case.seed,
            "boundary_step": upto_step,
            "source_dependencies": payload,
        }
    )


@dataclass(frozen=True)
class StepOutcome:
    record: StepRecord
    state: DecoderState
    candidate_score_count: int


def _target_embedding(
    case: TranslationCase,
    config: DecodeConfig,
    target_token: str,
) -> np.ndarray:
    return np.asarray(
        deterministic_embedding(
            case.seed, "target", target_token, config.embedding_dim
        ),
        dtype=np.float64,
    )


def _normalize(vector: np.ndarray) -> np.ndarray:
    norm = float(np.linalg.norm(vector))
    if norm <= 1e-15:
        return np.zeros_like(vector)
    return vector / norm


def _softmax(logits: list[float]) -> list[float]:
    if not logits:
        return []
    maximum = max(logits)
    exps = [math.exp(value - maximum) for value in logits]
    total = sum(exps)
    return [value / total for value in exps]


def rank_candidates(
    candidates: Sequence[CandidateRecord],
) -> tuple[CandidateRecord, ...]:
    """Rank by descending score, then target-token lexical order."""

    ranking = sorted(
        candidates, key=lambda candidate: (-candidate.score, candidate.token)
    )
    return tuple(
        replace(candidate, rank=index + 1)
        for index, candidate in enumerate(ranking)
    )


def _context_indices(
    alignments_at_step: Sequence[Alignment],
    target_step: int,
    source_length: int,
    context_window: int,
) -> tuple[int, ...]:
    if alignments_at_step:
        center = sorted({a.source_index for a in alignments_at_step})[0]
    elif source_length <= 1:
        center = 0
    else:
        # Only used in malformed/edge cases where a target position has no
        # alignment.  Valid cases always have at least one alignment.
        center = round(target_step * (source_length - 1) / max(1, target_step))
    lower = max(0, center - context_window)
    upper = min(source_length - 1, center + context_window)
    return tuple(range(lower, upper + 1))


def decode_step(
    case: TranslationCase,
    config: DecodeConfig,
    encoded: EncodedSource,
    state: DecoderState,
    alignments_by_target: dict[int, tuple[Alignment, ...]],
    target_vocab: tuple[str, ...],
    block_index: int,
) -> StepOutcome:
    """Decode exactly one step.

    Both whole-sentence and chunked paths call this function.  Block position is
    metadata only; ``state.step`` is always the global target position and the
    prefix normalization state is merged rather than reset.
    """

    step = state.step
    alignments_at_step = alignments_by_target.get(step, ())
    context_indices = _context_indices(
        alignments_at_step,
        step,
        len(case.source_tokens),
        config.context_window,
    )
    context_set = set(context_indices)

    current_vectors = [
        (
            alignment.source_index,
            np.asarray(
                encoded.vectors[alignment.source_index], dtype=np.float64
            ),
        )
        for alignment in alignments_at_step
        if alignment.source_index in context_set
    ]
    if current_vectors:
        weighted = np.zeros(config.embedding_dim, dtype=np.float64)
        weight_sum = 0.0
        for source_index, vector in current_vectors:
            weight = encoded.position_weights[source_index]
            weighted += weight * vector
            weight_sum += weight
        aligned_pool = _normalize(weighted / weight_sum)
    else:
        aligned_pool = np.zeros(config.embedding_dim, dtype=np.float64)

    previous_projection = np.asarray(
        state.previous_projection, dtype=np.float64
    )
    if state.previous_target_token is None:
        history_embedding = np.zeros(config.embedding_dim, dtype=np.float64)
    else:
        history_embedding = _target_embedding(
            case, config, state.previous_target_token
        )
    query = _normalize(
        0.62 * aligned_pool
        + 0.26 * previous_projection
        + 0.12 * history_embedding
    )

    raw_candidates: list[dict[str, Any]] = []
    all_attention_logits_for_selected: list[float] = []
    target_embeddings: dict[str, np.ndarray] = {}
    for token in target_vocab:
        embeddings = _target_embedding(case, config, token)
        target_embeddings[token] = embeddings
        records = tuple(
            alignment
            for alignment in alignments_at_step
            if alignment.target_token == token
            and alignment.source_index in context_set
        )
        if records:
            attention_logits = []
            record_vectors = []
            for alignment in records:
                source_vector = np.asarray(
                    encoded.vectors[alignment.source_index], dtype=np.float64
                )
                record_vectors.append(source_vector)
                attention_logits.append(
                    qfloat(
                        float(np.dot(query, source_vector))
                        / math.sqrt(config.embedding_dim)
                        + 0.15 * alignment.weight
                    )
                )
            attention_weights = _softmax(attention_logits)
            projection = np.zeros(config.embedding_dim, dtype=np.float64)
            for weight, vector in zip(attention_weights, record_vectors):
                projection += weight * vector
            projection = _normalize(projection)
        else:
            attention_logits = []
            attention_weights = []
            projection = np.zeros(config.embedding_dim, dtype=np.float64)

        has_current_alignment = bool(records)
        score = (
            (6.0 if has_current_alignment else 0.0)
            + 1.8 * sum(a.weight for a in records)
            + 1.1 * float(np.dot(embeddings, projection))
            + 0.9 * float(np.dot(embeddings, previous_projection))
            + 0.5 * float(np.dot(embeddings, query))
        )
        raw_candidates.append(
            {
                "token": token,
                "score": qfloat(score),
                "has_current_alignment": has_current_alignment,
                "aligned_source_indices": tuple(
                    sorted(a.source_index for a in records)
                ),
                "attention_weights": tuple(qfloat(v) for v in attention_weights),
                "attention_logits": attention_logits,
                "projection": qtuple(projection),
            }
        )

    probabilities = _softmax([candidate["score"] for candidate in raw_candidates])
    candidate_records: list[CandidateRecord] = []
    for probability, raw in zip(probabilities, raw_candidates):
        candidate_records.append(
            CandidateRecord(
                token=raw["token"],
                score=raw["score"],
                probability=qfloat(probability),
                rank=0,
                has_current_alignment=raw["has_current_alignment"],
                aligned_source_indices=raw["aligned_source_indices"],
                attention_weights=raw["attention_weights"],
            )
        )
    ranked = rank_candidates(candidate_records)
    winner = ranked[0]
    winner_raw = next(
        raw for raw in raw_candidates if raw["token"] == winner.token
    )
    all_attention_logits_for_selected = list(winner_raw["attention_logits"])

    confidence = next(
        candidate.probability
        for candidate in ranked
        if candidate.token == winner.token
    )
    projection = winner_raw["projection"]
    new_normalizer = qfloat(
        state.normalizer_sum
        + logsumexp([candidate.score for candidate in ranked])
    )
    attention_normalizer = (
        logsumexp(all_attention_logits_for_selected)
        if all_attention_logits_for_selected
        else 0.0
    )
    new_attention_sum = qfloat(
        state.attention_norm_sum + attention_normalizer
    )
    provisional = replace(
        state,
        step=step + 1,
        tokens=state.tokens + (winner.token,),
        confidences=state.confidences + (qfloat(confidence),),
        projections=state.projections + (projection,),
        previous_projection=projection,
        previous_target_token=winner.token,
        normalizer_sum=new_normalizer,
        attention_norm_sum=new_attention_sum,
        state_hash="",
    )
    new_state = replace(provisional, state_hash=provisional.compute_hash())
    input_version = {
        "case_id": case.case_id,
        "source_version": case.source_version,
        "align_version": case.align_version,
        "source_digest": case.source_digest,
        "encoding_digest": encoded.encoding_digest,
        "config_checksum": semantic_config_checksum(config),
    }
    record = StepRecord(
        step=step,
        block_index=block_index,
        token=winner.token,
        confidence=qfloat(confidence),
        projection=projection,
        candidates=ranked,
        candidate_log_normalizer=qfloat(
            logsumexp([candidate.score for candidate in ranked])
        ),
        attention_log_normalizer=qfloat(attention_normalizer),
        context_source_indices=context_indices,
        state_hash=new_state.state_hash,
        input_version=input_version,
    )
    return StepOutcome(
        record=record,
        state=new_state,
        candidate_score_count=len(target_vocab),
    )


def estimate_timing_ns(
    step_count: int,
    candidate_score_count: int,
    cache_entry_count: int,
) -> int:
    return int(
        _ESTIMATE_FIXED_NS
        + step_count * _ESTIMATE_STEP_NS
        + candidate_score_count * _ESTIMATE_CANDIDATE_NS
        + cache_entry_count * _ESTIMATE_CACHE_ENTRY_NS
    )


@dataclass(frozen=True)
class DecodeRangeOutcome:
    records: tuple[StepRecord, ...]
    states: tuple[DecoderState, ...]
    state: DecoderState
    observed_ns: int
    candidate_score_count: int


def decode_range(
    case: TranslationCase,
    config: DecodeConfig,
    encoded: EncodedSource,
    start_state: DecoderState,
    start_step: int,
    end_step: int,
    *,
    block_size: int,
) -> DecodeRangeOutcome:
    if start_state.step != start_step:
        raise ValueError(
            f"prefix state step {start_state.step} != range start {start_step}"
        )
    alignments_by_target = case.alignments_by_target()
    target_vocab = tuple(sorted(case.target_vocab))
    state = start_state
    records: list[StepRecord] = []
    states: list[DecoderState] = []
    candidate_scores = 0
    started = time.perf_counter_ns()
    for step in range(start_step, end_step):
        outcome = decode_step(
            case,
            config,
            encoded,
            state,
            alignments_by_target,
            target_vocab,
            step // block_size,
        )
        state = outcome.state
        records.append(outcome.record)
        states.append(state)
        candidate_scores += outcome.candidate_score_count
    observed = time.perf_counter_ns() - started
    return DecodeRangeOutcome(
        records=tuple(records),
        states=tuple(states),
        state=state,
        observed_ns=int(observed),
        candidate_score_count=int(candidate_scores),
    )


def decode_whole(
    case: TranslationCase,
    config: DecodeConfig,
    encoded: EncodedSource | None = None,
    *,
    cache_entry_count: int = 0,
) -> FullDecodeResult:
    encoded = encoded or encode_source(case, config)
    initial = DecoderState.initial(config.embedding_dim)
    outcome = decode_range(
        case,
        config,
        encoded,
        initial,
        0,
        case.target_length,
        block_size=config.block_size,
    )
    estimated = estimate_timing_ns(
        len(outcome.records),
        outcome.candidate_score_count,
        cache_entry_count,
    )
    timing = TimingRecord(
        stage="whole_decode",
        observed_ns=outcome.observed_ns,
        estimated_ns=estimated,
        step_count=len(outcome.records),
        candidate_score_count=outcome.candidate_score_count,
        cache_entry_count=cache_entry_count,
    )
    records_tuple = outcome.records
    tokens = tuple(record.token for record in records_tuple)
    confidences = tuple(record.confidence for record in records_tuple)
    projections = tuple(record.projection for record in records_tuple)
    intermediate_digest = sha256_value(semantic_step_payload(records_tuple))
    semantic_hash = stable_hash(
        "full-decode-v1",
        {
            "case_id": case.case_id,
            "source_version": case.source_version,
            "align_version": case.align_version,
            "source_digest": case.source_digest,
            "encoding_digest": encoded.encoding_digest,
            "config_checksum": semantic_config_checksum(config),
            "tokens": list(tokens),
            "confidences": [qfloat(v) for v in confidences],
            "projections": [list(qtuple(v)) for v in projections],
            "intermediate_digest": intermediate_digest,
        },
    )
    return FullDecodeResult(
        case_id=case.case_id,
        source_version=case.source_version,
        align_version=case.align_version,
        source_digest=case.source_digest,
        encoding_digest=encoded.encoding_digest,
        tokens=tokens,
        confidences=confidences,
        projections=projections,
        step_records=records_tuple,
        states=outcome.states,
        timing=timing,
        semantic_hash=semantic_hash,
        intermediate_digest=intermediate_digest,
    )


def _compare_float(expected: float, actual: float, precision: int) -> bool:
    tolerance = 10.0 ** (-precision)
    return abs(float(expected) - float(actual)) <= tolerance


def first_record_deviation(
    expected: Sequence[StepRecord],
    actual: Sequence[StepRecord],
    *,
    expected_offset: int = 0,
    precision: int,
    include_candidates: bool = True,
) -> dict[str, Any] | None:
    if len(expected) != len(actual):
        return {
            "step": expected_offset + min(len(expected), len(actual)),
            "field": "step_count",
            "expected": len(expected),
            "actual": len(actual),
        }
    for index, (left, right) in enumerate(zip(expected, actual)):
        step = expected_offset + index
        if left.step != right.step:
            return {
                "step": step,
                "field": "step",
                "expected": left.step,
                "actual": right.step,
            }
        if left.token != right.token:
            return {
                "step": step,
                "field": "token",
                "expected": left.token,
                "actual": right.token,
            }
        if not _compare_float(left.confidence, right.confidence, precision):
            return {
                "step": step,
                "field": "confidence",
                "expected": qfloat(left.confidence),
                "actual": qfloat(right.confidence),
            }
        if len(left.projection) != len(right.projection):
            return {
                "step": step,
                "field": "projection.length",
                "expected": len(left.projection),
                "actual": len(right.projection),
            }
        for dim, (a, b) in enumerate(zip(left.projection, right.projection)):
            if not _compare_float(a, b, precision):
                return {
                    "step": step,
                    "field": f"projection[{dim}]",
                    "expected": qfloat(a),
                    "actual": qfloat(b),
                }
        if not _compare_float(
            left.candidate_log_normalizer,
            right.candidate_log_normalizer,
            precision,
        ):
            return {
                "step": step,
                "field": "candidate_log_normalizer",
                "expected": qfloat(left.candidate_log_normalizer),
                "actual": qfloat(right.candidate_log_normalizer),
            }
        if not _compare_float(
            left.attention_log_normalizer,
            right.attention_log_normalizer,
            precision,
        ):
            return {
                "step": step,
                "field": "attention_log_normalizer",
                "expected": qfloat(left.attention_log_normalizer),
                "actual": qfloat(right.attention_log_normalizer),
            }
        if left.context_source_indices != right.context_source_indices:
            return {
                "step": step,
                "field": "context_source_indices",
                "expected": list(left.context_source_indices),
                "actual": list(right.context_source_indices),
            }
        if left.state_hash != right.state_hash:
            return {
                "step": step,
                "field": "state_hash",
                "expected": left.state_hash,
                "actual": right.state_hash,
            }
        if include_candidates:
            if len(left.candidates) != len(right.candidates):
                return {
                    "step": step,
                    "field": "candidates.length",
                    "expected": len(left.candidates),
                    "actual": len(right.candidates),
                }
            for candidate_index, (a, b) in enumerate(
                zip(left.candidates, right.candidates)
            ):
                if a.token != b.token:
                    return {
                        "step": step,
                        "field": f"candidates[{candidate_index}].token",
                        "expected": a.token,
                        "actual": b.token,
                    }
                if not _compare_float(a.score, b.score, precision):
                    return {
                        "step": step,
                        "field": f"candidates[{candidate_index}].score",
                        "expected": qfloat(a.score),
                        "actual": qfloat(b.score),
                    }
                if not _compare_float(a.probability, b.probability, precision):
                    return {
                        "step": step,
                        "field": f"candidates[{candidate_index}].probability",
                        "expected": qfloat(a.probability),
                        "actual": qfloat(b.probability),
                    }
    return None


def ensure_chunk_consistency(
    full: FullDecodeResult,
    chunk_records: Sequence[StepRecord],
    *,
    block_index: int,
    start_step: int,
    precision: int,
) -> None:
    expected = full.step_records[start_step : start_step + len(chunk_records)]
    deviation = first_record_deviation(
        expected,
        chunk_records,
        expected_offset=start_step,
        precision=precision,
    )
    if deviation is not None:
        fail(
            E_CHUNK_MISMATCH,
            "chunked decoding drifted from whole-sentence decoding",
            field=deviation["field"],
            data={
                "block_index": block_index,
                "first_deviation_step": deviation["step"],
                "deviation_field": deviation["field"],
                "expected": deviation["expected"],
                "actual": deviation["actual"],
                "completed_stage": "chunk_prefix_merge",
            },
        )


def records_semantic_hash(records: Sequence[StepRecord]) -> str:
    return sha256_value(
        canonical_json(
            {
                "kind": "step-records",
                "records": [record.to_dict(include_candidates=True) for record in records],
            }
        )
    )

