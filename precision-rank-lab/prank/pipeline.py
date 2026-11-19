"""End-to-end pipeline orchestration across the three precision tiers."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import numpy as np

from .config import Config, KNOWN_PRECISIONS
from .data_gen import read_jsonl
from .errors import BusinessError
from .features import FEATURE_NAMES, build_features, score_features, validate_candidates
from .models import CandidateSet
from .precision import make_context
from .ranking import (deterministic_order, diversity_rerank, gate_decisions,
                      )


@dataclass
class TierRun:
    mode: str
    query_id: str
    fixed_scale: int
    features: np.ndarray
    linear: np.ndarray
    scores: np.ndarray
    item_ids: list[str]
    gate_rows: list[dict]
    fine_order: list[str]
    rerank_order: list[str]
    adjusted_trace: list[float] = field(default_factory=list)


def run_tier(cand: CandidateSet, cfg: Config, mode: str) -> TierRun:
    ctx = make_context(mode, cfg.fixed)
    matrix, item_ids = build_features(cand, ctx)
    linear, scores = score_features(matrix, cfg.weights, cfg.bias, ctx)
    gate_rows = gate_decisions(scores, item_ids, ctx, cfg.threshold,
                               cfg.tolerance, cfg.gate_strategy,
                               cfg.near_threshold_delta)
    fine_order = _fine_top_n(scores, item_ids, gate_rows, cfg)
    items_by_id = {item.item_id: item for item in cand.items}
    native_scores = {item_ids[i]: scores[i] for i in range(len(item_ids))}
    rerank_order, trace = diversity_rerank(
        fine_order, items_by_id, native_scores, ctx,
        cfg.category_penalty, cfg.position_decay, cand.query.query_id,
    )
    scale = cfg.fixed.scale if mode == "fixed" else 1
    return TierRun(mode=mode, query_id=cand.query.query_id, fixed_scale=scale,
                   features=matrix, linear=linear, scores=scores,
                   item_ids=item_ids, gate_rows=gate_rows,
                   fine_order=fine_order, rerank_order=rerank_order,
                   adjusted_trace=trace)


def _fine_top_n(scores, item_ids, gate_rows, cfg: Config) -> list[str]:
    admitted = {row["item_id"] for row in gate_rows if row["admitted"]}
    order = deterministic_order(scores, item_ids)
    window = max(cfg.top_k, cfg.rerank_n)
    return [item_ids[i] for i in order if item_ids[i] in admitted][:window]


def run_query(cand: CandidateSet, cfg: Config, modes: list[str],
              validate: bool = True) -> dict[str, TierRun]:
    if validate:
        validate_candidates(cand, cfg.bounds)
    results: dict[str, TierRun] = {}
    for mode in modes:
        results[mode] = run_tier(cand, cfg, mode)
    return results


def execute_dataset(data_dir: str, cfg: Config, modes: list[str] | None = None,
                    ) -> dict[str, Any]:
    """Run every query on every tier, applying the configured degradation policy."""
    modes = modes or list(KNOWN_PRECISIONS)
    # fail fast on an unknown precision mode before touching the data
    for mode in modes:
        make_context(mode, cfg.fixed)
    candidates = read_jsonl(data_dir)
    per_query: list[dict[str, Any]] = []
    business_events: list[dict[str, Any]] = []
    degraded_queries: list[dict[str, Any]] = []

    for cand in candidates:
        qid = cand.query.query_id
        record: dict[str, Any] = {"query_id": qid, "tiers": {}, "status": "ok"}
        try:
            tier_runs = run_query(cand, cfg, modes, validate=True)
            record["tiers"] = {mode: _tier_payload(run) for mode, run in tier_runs.items()}
            record["_runs"] = tier_runs
        except BusinessError as exc:
            if cfg.degradation_policy == "abort":
                raise
            event = _handle_query_failure(exc, cand, cfg, modes)
            business_events.append(event["event"])
            if event["degraded"]:
                degraded_queries.append({"query_id": qid,
                                         "policy": cfg.degradation_policy,
                                         "upstream_codes": _code_chain(exc)})
                record["status"] = "degraded"
                record["degradation"] = event["degradation_marker"]
                record["tiers"] = {mode: _tier_payload(run)
                                   for mode, run in event["fallback_runs"].items()}
                record["_runs"] = event["fallback_runs"]
            else:
                record["status"] = "failed"
                record["error"] = exc.to_dict()
        per_query.append(record)

    return {
        "per_query": per_query,
        "business_events": business_events,
        "degraded_queries": degraded_queries,
        "n_queries": len(candidates),
        "modes": modes,
    }


def _handle_query_failure(exc: BusinessError, cand: CandidateSet,
                          cfg: Config, modes: list[str]) -> dict[str, Any]:
    event = {"event": exc.to_dict()}
    policy = cfg.degradation_policy
    if policy == "abort":
        event["degraded"] = False
        return event
    if policy == "skip_query":
        event["degraded"] = True
        event["degradation_marker"] = {
            "policy": "skip_query",
            "trigger_code": exc.code,
            "upstream_chain": _code_chain(exc),
        }
        event["fallback_runs"] = {}
        return event
    # fallback_float64: retry only with the reference tier and mark degradation
    fallback_modes = ["float64"]
    try:
        # precision-only fallback: input validation already ran upstream and
        # raised the root cause, do not re-validate the same bad datum.
        fallback_runs = run_query(cand, cfg, fallback_modes, validate=False)
    except BusinessError as retry_exc:
        chained = BusinessError.chain(
            BusinessError(retry_exc.code, "degradation",
                          f"fallback also failed: {retry_exc.message}",
                          query_id=cand.query.query_id, field=retry_exc.field,
                          context=retry_exc.context),
            exc,
        )
        event["event"] = chained.to_dict()
        event["degraded"] = False
        return event
    event["degraded"] = True
    event["degradation_marker"] = {
        "policy": "fallback_float64",
        "trigger_code": exc.code,
        "upstream_chain": _code_chain(exc),
        "fallback_tiers": fallback_modes,
    }
    event["fallback_runs"] = fallback_runs
    return event


def _code_chain(exc: BusinessError) -> list[str]:
    return [e.code for e in exc.upstream] + [exc.code]


def _tier_payload(run: TierRun) -> dict[str, Any]:
    def f64(value):
        return decode_native(value, run.mode, run.fixed_scale).tolist()
    return {
        "mode": run.mode,
        "fixed_scale": run.fixed_scale,
        "item_ids": run.item_ids,
        "features": f64(run.features),
        "linear": f64(run.linear),
        "scores": f64(run.scores),
        "gate": run.gate_rows,
        "fine_order": run.fine_order,
        "rerank_order": run.rerank_order,
        "adjusted_trace": run.adjusted_trace,
        "feature_columns": list(FEATURE_NAMES),
    }


def decode_native(value, mode: str, scale: int) -> np.ndarray:
    """Decode a native tier representation into a plain float64 ndarray."""
    if mode != "fixed":
        return np.asarray(value, dtype=np.float64)
    arr = np.asarray(value, dtype=object)
    decoded = np.empty(arr.shape, dtype=np.float64)
    for idx in np.ndindex(arr.shape):
        decoded[idx] = float(arr[idx]) / scale
    return decoded
