"""Aggregate tier runs into error-propagation statistics and the final report."""
from __future__ import annotations

import hashlib
import json
from typing import Any

import numpy as np

from .config import Config
from .error_analysis import (amplification_chain, attribute_dominant_source,
                             order_inversions, summarize_stage,
                             verify_fixed_recompute)
from .pipeline import TierRun
from .pipeline import decode_native
from .ranking import verify_monotonicity


def _stack(runs: list[TierRun], attr: str) -> tuple[np.ndarray, list]:
    arrays, labels = [], []
    for run in runs:
        raw = getattr(run, attr)
        value = decode_native(raw, run.mode, run.fixed_scale)
        arrays.append(value.reshape(-1))
        if attr == "features":
            labels.append([f"{iid}:f{col}" for iid in run.item_ids
                           for col in range(value.shape[1])])
        else:
            labels.append(run.item_ids)
    return np.concatenate(arrays), labels


def analyze_tier(ref_runs: list[TierRun], obs_runs: list[TierRun],
                 cfg: Config, mode: str) -> dict[str, Any]:
    quantiles = cfg.interval_quantiles
    stage_summaries: dict[str, dict] = {}

    # degraded queries may only carry the float64 fallback; restrict the
    # comparison to queries present in both tiers
    ref_ids = {_run_key(run) for run in ref_runs}
    obs_runs = [run for run in obs_runs if _run_key(run) in ref_ids]
    ref_runs = [run for run in ref_runs if _run_key(run) in
                {_run_key(r) for r in obs_runs}]

    ref_feat, feat_labels = _stack(ref_runs, "features")
    obs_feat, _ = _stack(obs_runs, "features")
    stage_summaries["features"] = {"stage": "features",
                                   **summarize_stage(ref_feat, obs_feat, feat_labels, quantiles)}

    ref_linear, lin_labels = _stack(ref_runs, "linear")
    obs_linear, _ = _stack(obs_runs, "linear")
    stage_summaries["linear"] = {"stage": "linear",
                                 **summarize_stage(ref_linear, obs_linear, lin_labels, quantiles)}

    ref_scores, score_labels = _stack(ref_runs, "scores")
    obs_scores, _ = _stack(obs_runs, "scores")
    stage_summaries["sigmoid"] = {"stage": "sigmoid",
                                  **summarize_stage(ref_scores, obs_scores, score_labels, quantiles)}

    # ranking / re-rank structural divergence, query by query
    fine_div, rerank_div = [], []
    gate_flip_rows = []
    for ref, obs in zip(ref_runs, obs_runs):
        fine_div.append(order_inversions(ref.fine_order, obs.fine_order))
        rerank_div.append(order_inversions(ref.rerank_order, obs.rerank_order))
        for ref_row, obs_row in zip(ref.gate_rows, obs.gate_rows):
            if ref_row["admitted"] != obs_row["admitted"]:
                gate_flip_rows.append({
                    "item_id": ref_row["item_id"],
                    "ref_admitted": ref_row["admitted"],
                    "obs_admitted": obs_row["admitted"],
                    "ref_score": ref_row["score"],
                    "obs_score": obs_row["score"],
                })
    ranking_divergence = _merge_divergence(fine_div)
    rerank_divergence = _merge_divergence(rerank_div)
    stage_summaries["ranking"] = {
        "stage": "ranking",
        "fine_rank": ranking_divergence,
        "rerank": rerank_divergence,
        "gate_admission_flips": gate_flip_rows,
    }

    chain = amplification_chain([stage_summaries["features"],
                                 stage_summaries["linear"],
                                 stage_summaries["sigmoid"]])
    attribution = attribute_dominant_source(
        stage_summaries,
        {"inversions": ranking_divergence["inversions"],
         "position_flips": ranking_divergence["position_flips"]},
    )

    fixed_check = None
    if mode == "fixed":
        fixed_check = verify_fixed_recompute(
            obs_feat, ref_feat, cfg.fixed.scale, cfg.fixed.rounding)

    monotone_ok = all(verify_monotonicity(run.adjusted_trace) for run in obs_runs)
    return {
        "tier": mode,
        "stages": stage_summaries,
        "amplification_chain": chain,
        "dominant_attribution": attribution,
        "fixed_recompute_check": fixed_check,
        "rerank_monotonicity_ok": monotone_ok,
    }


def _run_key(run: TierRun) -> tuple[str, ...]:
    return run.query_id


def _merge_divergence(parts: list[dict]) -> dict:
    return {
        "inversions": sum(p["inversions"] for p in parts),
        "position_flips": sum(p["position_flips"] for p in parts),
        "missing_in_observed": sorted({iid for p in parts
                                       for iid in p["missing_in_observed"]}),
        "extra_in_observed": sorted({iid for p in parts
                                     for iid in p["extra_in_observed"]}),
    }


def cross_level_checks(per_query: list[dict], cfg: Config) -> dict[str, Any]:
    """Requirement 8: cross-stage consistency self-checks."""
    query_sets = {}
    problems = []
    for record in per_query:
        if record.get("status") == "failed":
            continue
        qid = record["query_id"]
        tier_order_sets = {}
        for mode, payload in record["tiers"].items():
            fine = set(payload["fine_order"])
            rerank = set(payload["rerank_order"])
            if fine != rerank:
                problems.append({"check": "rerank_candidate_set",
                                 "query_id": qid, "tier": mode,
                                 "detail": "fine-rank and rerank sets differ"})
            if len(payload["rerank_order"]) != len(set(payload["rerank_order"])):
                problems.append({"check": "rerank_no_duplicates",
                                 "query_id": qid, "tier": mode,
                                 "detail": "duplicate item after rerank"})
            trace = payload.get("adjusted_trace", [])
            if trace and not verify_monotonicity(trace):
                problems.append({"check": "rerank_decay_monotonicity",
                                 "query_id": qid, "tier": mode,
                                 "detail": "position decay not monotonic per category"})
            repeated = [row for row in trace if row["category_penalty_applied"]]
            categories = [row["category"] for row in trace]
            if len(categories) != len(set(categories)) and not repeated:
                problems.append({"check": "rerank_category_penalty",
                                 "query_id": qid, "tier": mode,
                                 "detail": "repeated category without penalty flag"})
            tier_order_sets[mode] = fine
        query_sets[qid] = tier_order_sets
    # query_id set identical across tiers for every healthy query
    healthy = [r for r in per_query if r.get("status") == "ok"]
    ref_modes = set()
    for record in healthy:
        ref_modes.update(record.get("tiers", {}).keys())
    inconsistent_queries = [
        record["query_id"] for record in healthy
        if set(record.get("tiers", {}).keys()) != ref_modes
    ]
    consistent = not inconsistent_queries
    return {
        "query_id_sets_consistent_across_tiers": consistent,
        "inconsistent_queries": inconsistent_queries,
        "degraded_queries_excluded": [r["query_id"] for r in per_query
                                      if r.get("status") == "degraded"],
        "rerank_set_checks_passed": not problems,
        "problems": problems,
        "n_queries_checked": len(healthy),
    }


def threshold_stability_report(per_query: list[dict]) -> dict[str, Any]:
    """Compare strict vs tolerance-band decisions on threshold-near candidates."""
    near_rows = []
    strict_flips, band_flips = 0, 0
    for record in per_query:
        for mode, payload in record.get("tiers", {}).items():
            for row in payload["gate"]:
                if not row["near_threshold"]:
                    continue
                near_rows.append({
                    "query_id": record["query_id"],
                    "tier": mode,
                    "item_id": row["item_id"],
                    "score": row["score"],
                    "strict_pass": row["strict_pass"],
                    "band_pass": row["band_pass"],
                })
    # flips are measured against the float64 reference per item
    by_item: dict[str, dict] = {}
    for row in near_rows:
        key = f"{row['query_id']}:{row['item_id']}"
        by_item.setdefault(key, {})[row["tier"]] = row
    for key, tier_rows in by_item.items():
        if "float64" not in tier_rows:
            continue
        ref = tier_rows["float64"]
        for mode, row in tier_rows.items():
            if mode == "float64":
                continue
            if row["strict_pass"] != ref["strict_pass"]:
                strict_flips += 1
            if row["band_pass"] != ref["band_pass"]:
                band_flips += 1
    return {
        "near_threshold_candidates": len(by_item),
        "strict_strategy_cross_tier_flips": strict_flips,
        "band_strategy_cross_tier_flips": band_flips,
        "detail": near_rows,
    }


def build_report(dataset_result: dict, cfg: Config, seed: int,
                 data_dir: str, extra: dict | None = None) -> dict[str, Any]:
    per_query = dataset_result["per_query"]
    runs_by_mode: dict[str, list[TierRun]] = {mode: [] for mode in dataset_result["modes"]}
    serializable_queries = []
    for record in per_query:
        out = {k: v for k, v in record.items() if not k.startswith("_")}
        serializable_queries.append(out)
        for mode, run in record.get("_runs", {}).items():
            runs_by_mode[mode].append(run)

    ref_mode = cfg.raw.get("precision", {}).get("reference", "float64")
    tier_analysis = {}
    for mode in dataset_result["modes"]:
        if mode == ref_mode:
            continue
        tier_analysis[mode] = analyze_tier(runs_by_mode[ref_mode],
                                           runs_by_mode[mode], cfg, mode)

    report = {
        "report_schema_version": 1,
        "seed": seed,
        "data_dir": str(data_dir),
        "config_snapshot": {
            "config_version": cfg.config_version,
            "threshold": cfg.threshold,
            "tolerance": cfg.tolerance,
            "gate_strategy": cfg.gate_strategy,
            "top_k": cfg.top_k,
            "rerank_n": cfg.rerank_n,
            "weights": cfg.weights,
            "fixed": cfg.fixed.__dict__,
            "degradation_policy": cfg.degradation_policy,
        },
        "modes": dataset_result["modes"],
        "reference_mode": ref_mode,
        "n_queries": dataset_result["n_queries"],
        "tier_error_analysis": tier_analysis,
        "cross_level_checks": cross_level_checks(per_query, cfg),
        "threshold_stability": threshold_stability_report(per_query),
        "business_events": dataset_result["business_events"],
        "degraded_queries": dataset_result["degraded_queries"],
        "per_query": serializable_queries,
    }
    if extra:
        report["extra"] = extra
    report["report_hash"] = report_hash(report)
    return report


def report_hash(report: dict, alg: str = "sha256") -> str:
    """Hash over all content except the hash field; excludes wall-clock fields."""
    body = {k: v for k, v in report.items() if k not in ("report_hash",)}
    canonical = json.dumps(body, sort_keys=True, separators=(",", ":"),
                           ensure_ascii=False).encode("utf-8")
    return f"{alg}:" + hashlib.new(alg, canonical).hexdigest()
