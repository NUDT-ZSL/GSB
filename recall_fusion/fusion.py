"""Score normalization and weighted fusion with fully recomputable details."""

from __future__ import annotations

from .config import FusionConfig, NORMALIZATIONS
from .errors import ErrorCode, PipelineError
from .models import (
    Candidate,
    FusionDetail,
    FusedCandidate,
    MergedCandidate,
)


def validate_config(config: FusionConfig, query_id: str) -> None:
    for channel, channel_cfg in config.channels.items():
        norm = channel_cfg.normalization or config.scheduler.default_normalization
        if norm not in NORMALIZATIONS:
            raise PipelineError(
                ErrorCode.E_NORM_INVALID,
                stage="normalization",
                message=f"illegal normalization {norm!r} for channel {channel}",
                query_id=query_id,
                field=f"channels.{channel}.normalization",
            )
        if channel_cfg.weight < 0:
            raise PipelineError(
                ErrorCode.E_WEIGHT_INVALID,
                stage="fusion",
                message=(
                    f"negative fusion weight {channel_cfg.weight} for channel {channel}"
                ),
                query_id=query_id,
                field=f"channels.{channel}.weight",
            )


def _scale_scores(values: list[float]) -> list[float]:
    if not values:
        return []
    low, high = min(values), max(values)
    span = high - low
    if span == 0.0:
        return [1.0 for _ in values]
    return [(value - low) / span for value in values]


def _rank_reciprocal(
    candidates: list[Candidate], rank_constant: int
) -> dict[str, float]:
    ordered = sorted(candidates, key=lambda c: (-c.raw_score, c.doc_id))
    return {
        candidate.doc_id: 1.0 / (rank_constant + rank)
        for rank, candidate in enumerate(ordered, start=1)
    }


def build_normalizers(
    channel_candidates: dict[str, list[Candidate]],
    config: FusionConfig,
    query_id: str,
) -> dict[str, dict[str, float]]:
    validate_config(config, query_id)
    rank_constant = config.scheduler.rank_constant
    normalizers: dict[str, dict[str, float]] = {}
    for channel, candidates in channel_candidates.items():
        norm = config.channels[channel].normalization or (
            config.scheduler.default_normalization
        )
        if not candidates:
            normalizers[channel] = {}
        elif norm == "rank_reciprocal":
            normalizers[channel] = _rank_reciprocal(candidates, rank_constant)
        else:
            scaled = _scale_scores([c.raw_score for c in candidates])
            normalizers[channel] = {
                candidate.doc_id: value
                for candidate, value in zip(candidates, scaled)
            }
    return normalizers


def fuse(
    merged: list[MergedCandidate],
    channel_candidates: dict[str, list[Candidate]],
    config: FusionConfig,
    query_id: str,
) -> list[FusedCandidate]:
    normalizers = build_normalizers(channel_candidates, config, query_id)
    fused: list[FusedCandidate] = []

    for record in merged:
        details: list[FusionDetail] = []
        final_score = 0.0
        kept_channel = record.kept_channel
        for hit in record.hits:
            channel = hit.channel
            weight = config.channels[channel].weight
            normalized = normalizers[channel].get(record.doc_id, 0.0)
            # Only the policy-selected kept hit contributes to the fused score;
            # additional hits stay fully recorded with zero contribution.
            if channel != kept_channel:
                contribution = 0.0
            else:
                contribution = normalized * weight
            final_score += contribution
            details.append(
                FusionDetail(
                    channel=channel,
                    raw_score=hit.raw_score,
                    normalized_score=normalized,
                    weight=weight,
                    weighted_contribution=contribution,
                )
            )
        fused.append(
            FusedCandidate(
                doc_id=record.doc_id,
                final_score=final_score,
                kept_channel=kept_channel,
                details=details,
                all_hits=list(record.hits),
            )
        )

    fused.sort(key=lambda candidate: (-candidate.final_score, candidate.doc_id))
    return fused
