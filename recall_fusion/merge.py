"""Cross-channel alignment and doc_id-level deduplication."""

from __future__ import annotations

from .config import MERGE_POLICIES
from .errors import ErrorCode, PipelineError
from .models import Candidate, ChannelHit, MergedCandidate


def validate_merge_policy(policy: str, query_id: str) -> None:
    if policy not in MERGE_POLICIES:
        raise PipelineError(
            ErrorCode.E_MERGE_POLICY,
            stage="merge",
            message=(
                f"illegal merge_policy {policy!r}; expected one of {MERGE_POLICIES}"
            ),
            query_id=query_id,
            field="merge_policy",
        )


def merge_candidates(
    channel_candidates: dict[str, list[Candidate]],
    policy: str,
    query_id: str,
) -> list[MergedCandidate]:
    """Merge all channel hits keyed by doc_id.

    Every hit is retained on the merged record for audit. The policy selects the
    single "kept" hit that represents the document during normalization/fusion:
      * highest_normalized: the hit with the greatest raw score (normalized
        ordering is monotonic with raw ordering within a channel),
      * first_hit: the hit produced by the earliest configured channel.
    """
    validate_merge_policy(policy, query_id)

    grouped: dict[str, list[Candidate]] = {}
    channel_order = list(channel_candidates)
    for channel in channel_order:
        for candidate in channel_candidates[channel]:
            grouped.setdefault(candidate.doc_id, []).append(candidate)

    merged: list[MergedCandidate] = []
    for doc_id in sorted(grouped):
        hits_raw = grouped[doc_id]
        hits = [ChannelHit(channel=c.channel, raw_score=c.raw_score) for c in hits_raw]
        if policy == "first_hit":
            kept = min(hits_raw, key=lambda c: channel_order.index(c.channel))
        else:
            # highest raw score; ties broken by configured channel order.
            kept = min(
                hits_raw,
                key=lambda c: (-c.raw_score, channel_order.index(c.channel)),
            )
        merged.append(
            MergedCandidate(
                doc_id=doc_id,
                hits=hits,
                kept_channel=kept.channel,
                kept_raw_score=kept.raw_score,
            )
        )
    return merged
