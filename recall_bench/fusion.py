"""Fusion scoring and deterministic final ranking."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .channels import ChannelEngine, Candidate
from .config import CHANNEL_NAMES
from .parser import ParsedData
from .util import round_score


@dataclass
class ChannelResult:
    channel: str
    candidates: list[Candidate]
    skip_reason: str | None


class FusionEngine:
    def __init__(self, data: ParsedData, config: dict[str, Any], channels: ChannelEngine):
        self.data = data
        self.config = config
        self.channels = channels
        self.rrf_k = int(config["fusion"]["rrf_k"])
        self.mix = float(config["fusion"]["weighted_mix"])
        self.feature_weights = dict(config["feature_weights"])
        self.limits = dict(config["channel_limits"])
        self.max_epoch = max((row["ts_epoch"] for row in data.interactions), default=0.0)

    def recall_all(self, user_id: str) -> dict[str, ChannelResult]:
        results: dict[str, ChannelResult] = {}
        for channel in CHANNEL_NAMES:
            candidates, skip_reason = self.channels.run_channel(channel, user_id)
            results[channel] = ChannelResult(channel, candidates, skip_reason)
        return results

    def normalization_stats(self, channel_results: dict[str, dict[str, ChannelResult]]) -> dict[str, dict[str, float]]:
        stats: dict[str, dict[str, float]] = {}
        for channel in CHANNEL_NAMES:
            scores = [
                candidate.score
                for per_user in channel_results.get(channel, {}).values()
                for candidate in per_user.candidates
            ]
            if scores:
                stats[channel] = {"min": round_score(min(scores)), "max": round_score(max(scores))}
            else:
                stats[channel] = {"min": 0.0, "max": 0.0}
        return stats

    def _normalize(self, value: float, stat: dict[str, float]) -> float:
        lo, hi = stat["min"], stat["max"]
        if hi <= lo:
            return 1.0 if value > 0.0 else 0.0
        return (value - lo) / (hi - lo)

    def _redistribute_weights(
        self, active_channels: list[str], empty_channels: list[str]
    ) -> tuple[dict[str, float], list[dict[str, Any]]]:
        raw_weights = {name: float(self.config["channels"][name]["weight"]) for name in CHANNEL_NAMES}
        details: list[dict[str, Any]] = []
        if not empty_channels or not active_channels:
            return {name: raw_weights[name] for name in active_channels}, details
        freed = sum(raw_weights[name] for name in empty_channels)
        rule = self.config["fusion"]["redistribution"]
        active_total = sum(raw_weights[name] for name in active_channels)
        adjusted = {name: raw_weights[name] for name in active_channels}
        for empty_name in empty_channels:
            allocations: dict[str, float] = {}
            for name in active_channels:
                if rule == "equal" or active_total <= 0.0:
                    share = freed / len(active_channels)
                else:
                    share = freed * raw_weights[name] / active_total
                allocations[name] = round_score(share)
                adjusted[name] += share
            details.append({
                "empty_channel": empty_name,
                "redistributed_weight": round_score(freed),
                "rule": rule,
                "allocations": allocations,
            })
        return adjusted, details

    def fuse(
        self,
        user_id: str,
        recall: dict[str, ChannelResult],
        norm_stats: dict[str, dict[str, float]],
    ) -> dict[str, Any]:
        enabled = [name for name in CHANNEL_NAMES if self.config["channels"][name]["enabled"]]
        disabled = [name for name in CHANNEL_NAMES if not self.config["channels"][name]["enabled"]]
        empty = list(disabled) + [
            name for name in enabled if not recall[name].candidates
        ]
        active = [name for name in enabled if recall[name].candidates]
        weights, redistribution = self._redistribute_weights(active, empty)

        union_items = sorted({
            candidate.item_id
            for name in active
            for candidate in recall[name].candidates
        })
        rank_vectors: dict[str, tuple[int, ...]] = {}
        channel_contrib: dict[str, dict[str, Any]] = {}
        norm_values: dict[str, dict[str, float]] = {}

        for name in CHANNEL_NAMES:
            stat = norm_stats[name]
            per_item: dict[str, Any] = {}
            norms: dict[str, float] = {}
            channel_candidates = recall[name].candidates if name in recall else []
            for position, candidate in enumerate(channel_candidates, start=1):
                normalized = self._normalize(candidate.score, stat)
                per_item[candidate.item_id] = {
                    "rank": position,
                    "raw_score": round_score(candidate.score),
                    "norm_score": round_score(normalized),
                }
                norms[candidate.item_id] = normalized
            channel_contrib[name] = per_item
            norm_values[name] = norms

        for item_id in union_items:
            rank_vectors[item_id] = tuple(
                channel_contrib[name].get(item_id, {}).get("rank", len(recall[name].candidates) + 1)
                for name in CHANNEL_NAMES
            )

        strategy = self.config["fusion"]["strategy"]
        scored: list[dict[str, Any]] = []
        total_weight = sum(weights.values()) or 1.0
        for item_id in union_items:
            channel_part = 0.0
            rrf_part = 0.0
            for name in active:
                entry = channel_contrib[name].get(item_id)
                if entry is None:
                    continue
                normalized_weight = weights[name] / total_weight
                channel_part += normalized_weight * entry["norm_score"]
                rrf_part += normalized_weight * (1.0 / (self.rrf_k + entry["rank"]))
            freshness = self._freshness(item_id)
            feature_part = self._feature_part(item_id, norm_values, freshness)
            if strategy == "rrf":
                final_score = (1.0 - self.mix) * feature_part + self.mix * rrf_part
            else:
                final_score = self.mix * channel_part + (1.0 - self.mix) * feature_part
            scored.append({
                "item_id": item_id,
                "final_score": round_score(final_score),
                "channel_score": round_score(channel_part),
                "rrf_score": round_score(rrf_part),
                "freshness": round_score(freshness),
                "rank_vector": list(rank_vectors[item_id]),
            })

        scored.sort(key=lambda row: (-row["final_score"], row["rank_vector"], row["item_id"]))
        limit = int(self.config["fusion"]["final_list_limit"])
        final_rows = scored[:limit]
        for position, row in enumerate(final_rows, start=1):
            row["final_rank"] = position
            row["channel_contributions"] = {
                name: channel_contrib[name].get(row["item_id"])
                for name in CHANNEL_NAMES
                if self.config["channels"][name]["enabled"]
            }

        return {
            "user_id": user_id,
            "strategy": strategy,
            "redistribution": redistribution,
            "active_channels": active,
            "empty_channels": empty,
            "skipped_channels": {
                name: recall[name].skip_reason
                for name in CHANNEL_NAMES
                if recall[name].skip_reason is not None
            },
            "weights_after_redistribution": {
                name: round_score(weights.get(name, 0.0)) for name in CHANNEL_NAMES
            },
            "recommendations": final_rows,
        }

    def _feature_part(
        self,
        item_id: str,
        norm_values: dict[str, dict[str, float]],
        freshness: float,
    ) -> float:
        weights = self.feature_weights
        terms = [
            weights["lexical_norm"] * norm_values["lexical"].get(item_id, 0.0),
            weights["collaborative_norm"] * norm_values["collaborative"].get(item_id, 0.0),
            weights["popularity_norm"] * norm_values["popularity"].get(item_id, 0.0),
            weights["freshness"] * freshness,
        ]
        denom = sum(weights.values()) or 1.0
        return sum(terms) / denom

    def _freshness(self, item_id: str) -> float:
        latest = self.data.item_latest_epoch.get(item_id)
        if latest is None or self.max_epoch <= 0.0:
            return 0.0
        return max(0.0, min(1.0, latest / self.max_epoch))
