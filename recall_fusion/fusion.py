"""Cross-channel alignment, normalization, dedup and weighted fusion.

The fused score of every retained candidate is fully recomputable from its
``details`` rows: ``fused_score == sum(weight * norm_score)`` for rows that
participate under the configured dedup policy. Channels that did not hit are
recorded explicitly with null scores instead of being omitted.
"""

from __future__ import annotations

from dataclasses import dataclass

from .config import (
    CHANNEL_ORDER,
    Config,
    DEDUP_FIRST_HIT,
    NORM_MINMAX,
    NORM_RANK_INV,
)
from .errors import BusinessError, E_CONFIG_INVALID
from .recall import ChannelOutput


@dataclass
class FusedCandidate:
    doc_id: str
    fused_score: float
    primary_channel: str
    hit_channels: list[str]
    details: list[dict]
    final_rank: int = 0

    def to_dict(self) -> dict:
        return {
            "doc_id": self.doc_id,
            "fused_score": self.fused_score,
            "final_rank": self.final_rank,
            "primary_channel": self.primary_channel,
            "hit_channels": list(self.hit_channels),
            "details": self.details,
        }


def normalize_scores(output: ChannelOutput, norm_kind: str) -> list[float]:
    """Return one normalized score per candidate, in candidate order."""
    n = len(output.candidates)
    if n == 0:
        return []
    if norm_kind == NORM_RANK_INV:
        return [1.0 / candidate.rank for candidate in output.candidates]
    if norm_kind == NORM_MINMAX:
        raw = [candidate.raw_score for candidate in output.candidates]
        lo, hi = min(raw), max(raw)
        if hi == lo:
            return [1.0] * n
        span = hi - lo
        return [(value - lo) / span for value in raw]
    raise BusinessError(
        E_CONFIG_INVALID,
        f"normalization must be one of (minmax, rank_inverse), got {norm_kind!r}",
        stage="fusion",
        field="normalization",
    )


def _norm_kind_for(config: Config, channel: str) -> str:
    spec = config.channels[channel]
    return spec.normalization or config.normalization


def _build_rows(
    channel_outputs: dict[str, ChannelOutput], config: Config
) -> dict[str, dict[str, dict]]:
    rows: dict[str, dict[str, dict]] = {}
    for channel in CHANNEL_ORDER:
        output = channel_outputs[channel]
        norm_values = normalize_scores(output, _norm_kind_for(config, channel))
        for candidate, norm_value in zip(output.candidates, norm_values):
            rows.setdefault(candidate.doc_id, {})[channel] = {
                "raw_score": candidate.raw_score,
                "norm_score": float(norm_value),
                "rank": candidate.rank,
            }
    return rows


def _detail_rows(
    doc_id: str,
    hit_map: dict[str, dict],
    config: Config,
    participating: set[str],
) -> list[dict]:
    details = []
    for channel in CHANNEL_ORDER:
        spec = config.channels[channel]
        hit = channel in hit_map
        entry = hit_map.get(channel)
        norm_value = entry["norm_score"] if hit else None
        contribution = (
            spec.weight * norm_value
            if hit and channel in participating
            else 0.0
        )
        details.append(
            {
                "channel": channel,
                "hit": hit,
                "raw_score": entry["raw_score"] if hit else None,
                "norm_score": norm_value,
                "rank": entry["rank"] if hit else None,
                "weight": spec.weight,
                "contribution": float(contribution),
            }
        )
    return details


def _fuse(
    rows: dict[str, dict[str, dict]], config: Config, norm_scope_docs: set[str] | None
) -> list[FusedCandidate]:
    # Optional fine-stage: re-normalize within a restricted doc scope.
    if norm_scope_docs is not None:
        for channel in CHANNEL_ORDER:
            kind = _norm_kind_for(config, channel)
            scoped_docs = [
                (doc_id, hit_map[channel])
                for doc_id, hit_map in rows.items()
                if doc_id in norm_scope_docs and channel in hit_map
            ]
            if kind == NORM_RANK_INV:
                scoped_docs.sort(key=lambda item: item[1]["rank"])
                values = {
                    doc_id: 1.0 / (position + 1)
                    for position, (doc_id, _) in enumerate(scoped_docs)
                }
            else:
                raw_values = [entry["raw_score"] for _, entry in scoped_docs]
                if raw_values:
                    lo, hi = min(raw_values), max(raw_values)
                else:
                    lo, hi = 0.0, 0.0
                values = {}
                for doc_id, entry in scoped_docs:
                    if hi == lo:
                        values[doc_id] = 1.0
                    else:
                        values[doc_id] = (entry["raw_score"] - lo) / (hi - lo)
            for doc_id, value in values.items():
                rows[doc_id][channel]["norm_score"] = float(value)

    candidates: list[FusedCandidate] = []
    for doc_id, hit_map in rows.items():
        hit_channels = [c for c in CHANNEL_ORDER if c in hit_map]
        if config.dedup_policy == DEDUP_FIRST_HIT:
            primary = hit_channels[0]
            participating = {primary}
        else:  # highest_normalized: earliest channel wins exact ties
            primary = max(
                hit_channels,
                key=lambda channel: (hit_map[channel]["norm_score"], -CHANNEL_ORDER.index(channel)),
            )
            participating = set(hit_channels)
        details = _detail_rows(doc_id, hit_map, config, participating)
        fused_score = sum(row["contribution"] for row in details)
        candidates.append(
            FusedCandidate(
                doc_id=doc_id,
                fused_score=float(fused_score),
                primary_channel=primary,
                hit_channels=hit_channels,
                details=details,
            )
        )
    candidates.sort(key=lambda item: (-item.fused_score, item.doc_id))
    for position, item in enumerate(candidates):
        item.final_rank = position + 1
    return candidates


def _rerank_fine(
    rows: dict[str, dict[str, dict]],
    coarse: list[FusedCandidate],
    config: Config,
) -> list[FusedCandidate]:
    """Two-stage fine rerank over a restricted scope.

    Coarse scores/details stay intact on every candidate. The fine pass builds
    an independent scoped normalization and its own recomputable details, then
    replaces only the head ordering; the tail keeps the coarse ordering.
    """
    scope = {item.doc_id for item in coarse[: max(1, config.coarse_k)]}
    scoped_rows = {
        doc_id: {channel: dict(entry) for channel, entry in hit_map.items()}
        for doc_id, hit_map in rows.items()
        if doc_id in scope
    }
    # Re-normalize inside the fine scope.
    for channel in CHANNEL_ORDER:
        kind = _norm_kind_for(config, channel)
        entries = [
            (doc_id, hit_map[channel])
            for doc_id, hit_map in scoped_rows.items()
            if channel in hit_map
        ]
        if kind == NORM_RANK_INV:
            entries.sort(key=lambda item: item[1]["rank"])
            for position, (doc_id, _) in enumerate(entries):
                scoped_rows[doc_id][channel]["norm_score"] = 1.0 / (position + 1)
        else:
            raw = [entry["raw_score"] for _, entry in entries]
            lo, hi = (min(raw), max(raw)) if raw else (0.0, 0.0)
            for doc_id, entry in entries:
                entry["norm_score"] = 1.0 if hi == lo else (entry["raw_score"] - lo) / (hi - lo)

    fine_candidates = _fuse(scoped_rows, config, norm_scope_docs=None)
    fine_by_id = {item.doc_id: item for item in fine_candidates}

    head: list[FusedCandidate] = []
    for coarse_item in coarse:
        if coarse_item.doc_id not in scope:
            continue
        fine_item = fine_by_id[coarse_item.doc_id]
        head.append(
            FusedCandidate(
                doc_id=coarse_item.doc_id,
                fused_score=fine_item.fused_score,
                primary_channel=fine_item.primary_channel,
                hit_channels=fine_item.hit_channels,
                details=fine_item.details,
            )
        )
    # Preserve the fine-pass evidence alongside the recomputable coarse detail.
    coarse_by_id = {item.doc_id: item for item in coarse}
    for item in head:
        item.details = [
            {
                **row,
                "coarse_norm_score": next(
                    detail["norm_score"]
                    for detail in coarse_by_id[item.doc_id].details
                    if detail["channel"] == row["channel"]
                ),
                "coarse_contribution": next(
                    detail["contribution"]
                    for detail in coarse_by_id[item.doc_id].details
                    if detail["channel"] == row["channel"]
                ),
            }
            for row in item.details
        ]
    tail = [item for item in coarse if item.doc_id not in scope]
    merged = head + tail
    for position, item in enumerate(merged):
        item.final_rank = position + 1
    return merged


def run_fusion(
    channel_outputs: dict[str, ChannelOutput],
    config: Config,
    two_stage: bool,
) -> tuple[list[FusedCandidate], int, str]:
    """Return fused candidates, comparisons consumed, and the stage mode."""
    rows = _build_rows(channel_outputs, config)
    aligned_count = len(rows)
    coarse = _fuse(rows, config, norm_scope_docs=None)
    if not two_stage or aligned_count <= config.fine_k:
        return coarse, aligned_count, (
            "two_stage_skipped_small" if two_stage else "single_stage"
        )
    merged = _rerank_fine(rows, coarse, config)
    fine_scope_size = min(len(coarse), max(1, config.coarse_k))
    comparisons = aligned_count + fine_scope_size
    return merged, comparisons, "two_stage"
