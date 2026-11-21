"""Aggregate recall statistics and construction of versioned snapshot payloads.

The three payloads produced here (recall stats, normalization parameters,
fusion weights) are content-addressed by :mod:`recall_fusion.snapshots`,
giving the experiment run a fully replayable parameter/statistics bundle.
"""

from __future__ import annotations

from typing import Any

from .config import CHANNEL_ORDER, Config


def new_channel_accumulator() -> dict[str, dict[str, Any]]:
    return {
        channel: {
            "requests": 0,
            "candidates_emitted": 0,
            "matched_total": 0,
            "truncated_queries": 0,
            "empty_queries": 0,
        }
        for channel in CHANNEL_ORDER
    }


def accumulate(
    accum: dict[str, dict[str, Any]],
    channel_outputs: dict[str, Any],
    executed_channels: list[str],
) -> None:
    for channel in executed_channels:
        output = channel_outputs[channel]
        bucket = accum[channel]
        bucket["requests"] += 1
        candidates = output["candidates"]
        bucket["candidates_emitted"] += len(candidates)
        bucket["matched_total"] += output["matched_total"]
        bucket["truncated_queries"] += 1 if output["truncated"] else 0
        bucket["empty_queries"] += 1 if not candidates else 0


def build_recall_stats_payload(
    accum: dict[str, dict[str, Any]],
    query_count: int,
    admitted_count: int,
    rejected_count: int,
) -> dict[str, Any]:
    channels: dict[str, Any] = {}
    for channel in CHANNEL_ORDER:
        bucket = accum[channel]
        requests = bucket["requests"]
        channels[channel] = {
            **bucket,
            "avg_candidates_per_request": (
                round(bucket["candidates_emitted"] / requests, 6)
                if requests
                else 0.0
            ),
            "truncation_rate": (
                round(bucket["truncated_queries"] / requests, 6) if requests else 0.0
            ),
        }
    return {
        "query_count": query_count,
        "admitted_count": admitted_count,
        "rejected_count": rejected_count,
        "channels": channels,
    }


def build_norm_params_payload(config: Config) -> dict[str, Any]:
    return {
        "global_normalization": config.normalization,
        "per_channel": {
            channel: config.channels[channel].normalization or config.normalization
            for channel in CHANNEL_ORDER
        },
        "dedup_policy": config.dedup_policy,
        "precision": config.precision,
    }


def build_fusion_weights_payload(config: Config) -> dict[str, Any]:
    return {
        channel: {
            "weight": config.channels[channel].weight,
            "candidate_limit": config.channels[channel].candidate_limit,
            "enabled": config.channels[channel].enabled,
        }
        for channel in CHANNEL_ORDER
    }
