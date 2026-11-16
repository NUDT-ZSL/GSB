"""Deterministic strategy-table scoring for sparse row blocking."""

from __future__ import annotations

import math
from typing import Any

import numpy as np

from .csr import CSRMatrix
from .errors import ConfigError, StrategyError
from .kernels import VALID_ORDERS
from .utils import stable_text_hash


def compute_metrics(csr: CSRMatrix, B: np.ndarray, config: dict[str, Any]) -> dict[str, Any]:
    row_nnz = csr.row_nnz().astype(np.float64)
    mean_nnz = float(row_nnz.mean()) if row_nnz.size else 0.0
    std_nnz = float(row_nnz.std()) if row_nnz.size else 0.0
    b_abs = np.abs(B.astype(np.float64))
    positive = b_abs[b_abs > 0.0]
    dynamic_range = float(positive.max() / positive.min()) if positive.size else 1.0
    distribution = config.get("structure", {}).get("distribution", "unknown")
    return {
        "M": int(csr.rows),
        "K": int(csr.cols),
        "N": int(B.shape[1]),
        "nnz": int(csr.nnz),
        "distribution": distribution,
        "avg_nnz_per_row": mean_nnz,
        "max_nnz_per_row": int(row_nnz.max()) if row_nnz.size else 0,
        "nnz_coefficient_of_variation": std_nnz / mean_nnz if mean_nnz else 0.0,
        "zero_row_count": int(np.count_nonzero(row_nnz == 0)),
        "b_global_magnitude_ratio": dynamic_range,
        "b_global_log10_magnitude_ratio": math.log10(max(dynamic_range, 1.0)),
    }


def _target_block_size(metrics: dict[str, Any]) -> int:
    max_nnz = int(metrics["max_nnz_per_row"])
    distribution = metrics["distribution"]
    target = 32
    while target < 256 and target * 4 < max_nnz:
        target *= 2
    if distribution == "powerlaw" and target > 64:
        target //= 2
    return target


def _order_fit(order: str, magnitude_score: float, chain_score: float) -> float:
    if order == "asc":
        return 0.62 - 0.20 * magnitude_score + 0.08 * (1.0 - chain_score)
    if order == "desc":
        return 0.60 + 0.10 * magnitude_score + 0.05 * chain_score
    return 0.60 + 0.32 * magnitude_score + 0.08 * chain_score


def select_strategy(
    csr: CSRMatrix,
    B: np.ndarray,
    config: dict[str, Any],
    strategy_config: dict[str, Any],
) -> dict[str, Any]:
    candidates = strategy_config.get("candidates")
    if not isinstance(candidates, list) or not candidates:
        raise ConfigError("strategy candidates must be a non-empty list", stage="strategy-config")
    metrics = compute_metrics(csr, B, config)
    target = _target_block_size(metrics)
    magnitude_score = min(1.0, metrics["b_global_log10_magnitude_ratio"] / 12.0)
    chain_score = min(1.0, math.log2(metrics["max_nnz_per_row"] + 1.0) / 12.0)
    distribution = metrics["distribution"]
    evaluated: list[dict[str, Any]] = []
    seen_ids: set[str] = set()
    for candidate in candidates:
        if not isinstance(candidate, dict):
            raise ConfigError("strategy candidate must be an object", stage="strategy-config")
        candidate_id = str(candidate.get("id", ""))
        if not candidate_id or candidate_id in seen_ids:
            raise StrategyError(
                "strategy candidate ids must be unique and non-empty",
                stage="strategy-config",
                details={"id": candidate_id},
            )
        seen_ids.add(candidate_id)
        raw_block_size = candidate.get("block_size")
        if isinstance(raw_block_size, bool) or not isinstance(raw_block_size, (int, np.integer)):
            raise StrategyError(
                "strategy candidate block_size must be an integer",
                stage="strategy-config",
                details={"id": candidate_id, "block_size": repr(raw_block_size)},
            )
        block_size = int(raw_block_size)
        if block_size <= 0:
            raise StrategyError(
                "strategy candidate block_size must be positive",
                stage="strategy-config",
                details={"id": candidate_id, "block_size": block_size},
            )
        eligible = block_size <= csr.cols
        order = str(candidate.get("order", ""))
        if order not in VALID_ORDERS:
            raise StrategyError(
                "strategy candidate has an invalid order",
                stage="strategy-config",
                details={"id": candidate_id, "order": order},
            )
        prior = float(candidate.get("prior", 0.0))
        block_fit = 1.0 / (1.0 + abs(math.log2(block_size / target)))
        order_fit = _order_fit(order, magnitude_score, chain_score)
        distribution_fit = 0.5
        if distribution == "extreme" and block_size >= 64:
            distribution_fit += 0.2
        if distribution == "powerlaw" and 32 <= block_size <= 128:
            distribution_fit += 0.2
        if distribution == "uniform" and block_size <= 64:
            distribution_fit += 0.15
        score = 0.52 * block_fit + 0.34 * order_fit + 0.09 * distribution_fit + 0.05 * prior
        candidate_record = {
                "id": candidate_id,
                "block_size": block_size,
                "order": order,
                "prior": prior,
                "eligible": bool(eligible),
                "score_components": {
                    "block_fit": block_fit,
                    "order_fit": order_fit,
                    "distribution_fit": distribution_fit,
                    "prior": prior,
                },
                "score": score if eligible else 0.0,
            }
        if not eligible:
            candidate_record["excluded_reason"] = "block_size exceeds K"
        evaluated.append(candidate_record)
    eligible_candidates = [candidate for candidate in evaluated if candidate["eligible"]]
    if not eligible_candidates:
        raise StrategyError(
            "no strategy candidate is valid for this K",
            stage="strategy-config",
            details={"K": csr.cols},
        )
    evaluated.sort(key=lambda item: (not item["eligible"], -item["score"], item["id"]))
    selected = evaluated[0]
    signature = stable_text_hash(
        [
            selected["id"],
            str(selected["block_size"]),
            selected["order"],
            str(metrics["nnz"]),
            str(metrics["M"]),
            str(metrics["K"]),
            str(metrics["N"]),
        ]
    )
    reason = (
        f"Selected {selected['id']} (block_size={selected['block_size']}, order={selected['order']}) "
        f"with score {selected['score']:.6f}; target block size {target}, max row nnz "
        f"{metrics['max_nnz_per_row']}, B log10 range {metrics['b_global_log10_magnitude_ratio']:.3f}, "
        f"distribution {distribution}."
    )
    return {
        "reference_metrics": metrics,
        "target_block_size": target,
        "candidates": evaluated,
        "strategy_id": selected["id"],
        "block_size": selected["block_size"],
        "order": selected["order"],
        "score": selected["score"],
        "reason": reason,
        "strategy_signature": signature,
    }
