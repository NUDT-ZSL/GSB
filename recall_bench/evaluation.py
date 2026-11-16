"""Offline evaluation with deterministic holdout split and metrics."""

from __future__ import annotations

import random
from typing import Any

from .channels import ChannelEngine
from .config import CHANNEL_NAMES
from .fusion import FusionEngine
from .parser import ParsedData


def sample_users(data: ParsedData, ratio: float, seed: int) -> list[str]:
    rng = random.Random(seed + 7)
    users = sorted(data.user_interactions.keys())
    count = max(1, round(len(users) * ratio)) if users else 0
    return sorted(rng.sample(users, count)) if count else []


def holdout_split(
    data: ParsedData, user_id: str, holdout_n: int
) -> tuple[set[str], set[str]]:
    records = data.user_interactions[user_id]
    ordered = sorted(records, key=lambda row: (row["ts_epoch"], row["item_id"]))
    holdout = {row["item_id"] for row in ordered[-holdout_n:]} if holdout_n > 0 else set()
    train = {row["item_id"] for row in ordered[:-holdout_n]} if holdout_n else {row["item_id"] for row in ordered}
    return train, holdout


def evaluate_scale(
    data: ParsedData,
    fused_users: dict[str, dict[str, Any]],
    config: dict[str, Any],
    declared_users: int,
) -> dict[str, Any]:
    eval_cfg = config["evaluation"]
    cutoffs = list(eval_cfg["cutoffs"])
    holdout_n = int(eval_cfg["holdout_last_n"])
    sampled = sample_users(data, float(eval_cfg["sample_ratio"]), int(config["seed"]))

    train_view = _build_train_view(data, sampled, holdout_n)
    train_engine = ChannelEngine(train_view, config)
    train_fusion = FusionEngine(train_view, config, train_engine)
    train_recall: dict[str, dict[str, Any]] = {}
    eval_user_errors: list[dict[str, str]] = []
    for user_id in sampled:
        try:
            train_recall[user_id] = train_fusion.recall_all(user_id)
        except Exception as exc:  # noqa: BLE001 - per-user isolation
            eval_user_errors.append({
                "user_id": user_id,
                "stage": "evaluation",
                "code": "E_STAGE_USER",
                "exception_type": type(exc).__name__,
                "message": str(exc),
            })
            train_recall[user_id] = _empty_recall(config)
    norm_stats = train_fusion.normalization_stats(
        {
            channel: {
                user_id: train_recall[user_id][channel] for user_id in sampled
            }
            for channel in CHANNEL_NAMES
        }
    )

    per_cutoff = {cutoff: {"hit_users": 0, "recall_sum": 0.0, "precision_sum": 0.0} for cutoff in cutoffs}
    covered_items: set[str] = set()
    sampled_with_holdout = 0
    user_results: dict[str, dict[str, Any]] = {}

    for user_id in sampled:
        try:
            user_results[user_id] = train_fusion.fuse(
                user_id, train_recall[user_id], norm_stats
            )
        except Exception as exc:  # noqa: BLE001 - per-user isolation
            eval_user_errors.append({
                "user_id": user_id, "stage": "evaluation", "code": "E_STAGE_USER",
                "exception_type": type(exc).__name__, "message": str(exc),
            })
            continue
        _train, holdout = holdout_split(data, user_id, holdout_n)
        if not holdout:
            continue
        sampled_with_holdout += 1
        train_result = user_results[user_id]
        recommended = [
            row["item_id"]
            for row in train_result["recommendations"]
        ]
        for cutoff in cutoffs:
            top = recommended[:cutoff]
            hits = len(holdout.intersection(top))
            if hits:
                per_cutoff[cutoff]["hit_users"] += 1
            per_cutoff[cutoff]["recall_sum"] += hits / len(holdout)
            per_cutoff[cutoff]["precision_sum"] += hits / cutoff
        covered_items.update(recommended)

    denominator = max(sampled_with_holdout, 1)
    metrics: dict[str, Any] = {}
    for cutoff in cutoffs:
        bucket = per_cutoff[cutoff]
        metrics[f"recall@{cutoff}"] = round(bucket["recall_sum"] / denominator, 10)
        metrics[f"precision@{cutoff}"] = round(bucket["precision_sum"] / denominator, 10)
        metrics[f"hit_rate@{cutoff}"] = round(bucket["hit_users"] / denominator, 10)

    all_recommended = set()
    for user_id in sampled:
        train_result = user_results.get(user_id)
        if train_result is None:
            continue
        all_recommended.update(row["item_id"] for row in train_result["recommendations"])
    long_tail_items = {
        item_id for item_id, count in data.item_interaction_counts.items() if count == 1
    }
    catalog_items = set(data.item_terms)
    metrics["catalog_coverage"] = round(len(all_recommended) / max(len(catalog_items), 1), 10)
    metrics["long_tail_coverage"] = round(
        len(all_recommended.intersection(long_tail_items)) / max(len(long_tail_items), 1), 10
    )
    cold_users = [user_id for user_id in sampled if not train_view.user_items.get(user_id)]
    metrics["cold_user_empty_rate"] = round(
        sum(
            1
            for user_id in cold_users
            if not (user_results.get(user_id) or {}).get("recommendations")
        )
        / max(len(cold_users), 1),
        10,
    )
    metrics["sampled_users"] = len(sampled)
    metrics["evaluated_users"] = sampled_with_holdout
    declared_cold = [
        f"user_{idx:04d}"
        for idx in range(int(declared_users))
        if f"user_{idx:04d}" not in data.user_interactions
    ]
    metrics["cold_user_count"] = len(declared_cold)
    cold_with_recs = 0
    for user_id in declared_cold:
        train_recall_cold = train_fusion.recall_all(user_id)
        cold_result = train_fusion.fuse(user_id, train_recall_cold, norm_stats)
        if cold_result["recommendations"]:
            cold_with_recs += 1
    metrics["declared_cold_empty_rate"] = round(
        (len(declared_cold) - cold_with_recs) / max(len(declared_cold), 1), 10
    )
    return {
        "metrics": metrics,
        "sampled_users": sampled,
        "eval_user_errors": eval_user_errors,
    }



def _empty_recall(config: dict[str, Any]) -> dict[str, Any]:
    from .fusion import ChannelResult

    return {
        name: ChannelResult(name, [], "user_exception")
        for name in CHANNEL_NAMES
    }


def _build_train_view(data: ParsedData, sampled_users: list[str], holdout_n: int) -> ParsedData:
    """Remove each sampled user's last-N interactions to emulate unseen items."""
    removed: set[tuple[str, str]] = set()
    for user_id in sampled_users:
        ordered = sorted(
            data.user_interactions[user_id],
            key=lambda row: (row["ts_epoch"], row["item_id"]),
        )
        tail = ordered[-holdout_n:] if holdout_n > 0 else []
        for row in tail:
            removed.add((user_id, row["item_id"]))

    view = ParsedData()
    view.item_terms = dict(data.item_terms)
    view.interactions = [
        row for row in data.interactions
        if (row["user_id"], row["item_id"]) not in removed
    ]
    view.failures = list(data.failures)
    from .parser import _build_indices

    _build_indices(view)
    return view
