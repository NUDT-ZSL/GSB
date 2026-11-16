"""Scale adaptive strategy selection from a deterministic preset table."""
from __future__ import annotations

from dataclasses import dataclass

from .combiner import CrossResult
from .config import Config, ScaleSpec
from .encoder import EncodedMatrix
from .hashing import digest_iterable

SMALL_SAMPLE_RATIO = 5.0


@dataclass
class ScaleMetrics:
    n_samples: int
    n_features: int
    small_sample_ratio: float
    budget_occupancy: float
    active_crosses: int
    degraded_crosses: int

    def as_dict(self) -> dict:
        return {
            "n_samples": self.n_samples,
            "n_features": self.n_features,
            "small_sample_ratio": self.small_sample_ratio,
            "budget_occupancy": self.budget_occupancy,
            "active_crosses": self.active_crosses,
            "degraded_crosses": self.degraded_crosses,
        }


def compute_metrics(scale: ScaleSpec, encoded: EncodedMatrix,
                    crosses: CrossResult, config: Config) -> ScaleMetrics:
    n_samples, n_features = encoded.matrix.shape
    ratio = float(n_features) / float(max(1, n_samples))
    if crosses.cross_columns:
        occupancy = sum(item.raw_cardinality / item.budget
                        for item in crosses.cross_columns) / len(crosses.cross_columns)
    else:
        occupancy = 0.0
    degraded = sum(1 for item in crosses.cross_columns
                   if item.strategy != "kept")
    return ScaleMetrics(
        n_samples=n_samples, n_features=n_features, small_sample_ratio=ratio,
        budget_occupancy=float(min(1.0, occupancy)),
        active_crosses=len(crosses.cross_columns), degraded_crosses=degraded,
    )


def select_strategy(config: Config, scale: ScaleSpec,
                    metrics: ScaleMetrics) -> dict:
    candidates = []
    for row in config.strategy_table:
        sample_ok = row["n_ge"] <= metrics.n_samples < row["n_lt"]
        feature_ok = metrics.n_features >= row["p_gt"]
        occupancy_ok = metrics.budget_occupancy >= row["occupancy_gt"]
        ratio_ok = metrics.small_sample_ratio >= row["small_sample_gt"]
        gates = {
            "sample_window": sample_ok,
            "feature_threshold": feature_ok,
            "occupancy_threshold": occupancy_ok,
            "small_sample_threshold": ratio_ok,
        }
        fit_score = (
            (1.0 if sample_ok else 0.0) * 4.0
            + (1.0 if feature_ok else 0.0) * 2.0
            + (1.0 if occupancy_ok else 0.0) * 2.0
            + (1.0 if ratio_ok else 0.0) * 2.0
            + min(1.0, metrics.small_sample_ratio / SMALL_SAMPLE_RATIO)
        )
        candidates.append({
            "name": row["name"],
            "path": row["path"],
            "max_order": row["max_order"],
            "gates": gates,
            "fit_score": round(float(fit_score), 6),
            "eligible": all(gates.values()),
        })
    eligible = [item for item in candidates if item["eligible"]]
    pool = eligible or candidates
    chosen = sorted(
        pool,
        key=lambda item: (-item["fit_score"], item["path"], item["name"]),
    )[0]
    capped_order = min(config.cross.max_order, chosen["max_order"])
    reason = (
        "all gates passed" if chosen["eligible"]
        else "no candidate passed every gate; highest fit score selected"
    )
    decision = {
        "scale_id": scale.id,
        "metrics": metrics.as_dict(),
        "candidate_scores": candidates,
        "selected_path": chosen["path"],
        "selected_max_order": capped_order,
        "strategy_name": chosen["name"],
        "selection_reason": reason,
    }
    decision["decision_fingerprint"] = digest_iterable([
        config.config_fingerprint, scale.id, decision,
    ])
    return decision
