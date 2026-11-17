"""逐级误差传播追踪：配置档位 vs 高一档位完整重算。

高档位产物先定点缩放到配置档位量纲再逐候选比较，保证差值是同一 LSB 下的
真实误差，而不是不同位宽的尺度差。
"""

from .fixedpoint import FixedSpec, scale_int


def run_pipeline_at_spec(request, feature_names, config, groups, spec: FixedSpec,
                         threshold_override=None):
    """在指定位宽下完整执行归一化、融合、重排，供双档对比复用。"""
    from .fusion import fuse_request
    from .normalize import normalize_request
    from .pipeline import prepare_candidates
    from .rerank import rerank_request

    recall = [n for n in feature_names if n.startswith("recall_")]
    dense = [n for n in feature_names if n.startswith("dense_")]
    sparse = [n for n in feature_names if n.startswith("sparse_")]
    if request.get("candidates") and "dense_map" not in request["candidates"][0]:
        request = prepare_candidates(request, feature_names, recall, dense, sparse)

    norm = normalize_request(request, feature_names, config["normalization"], spec)
    fused = fuse_request(
        norm, config["weights"], config["temperature"], config["score_clip"], spec
    )
    item_ids = [c["item_id"] for c in request["candidates"]]
    scores = [f["score"] for f in fused]
    rerank = rerank_request(item_ids, norm["codes"], scores, groups, config, spec,
                            threshold_override=threshold_override)
    return norm, fused, rerank


def _scale_to_config(value, high_bits, config_bits, rounding):
    if isinstance(value, list):
        return [_scale_to_config(v, high_bits, config_bits, rounding) for v in value]
    scaled, _ = scale_int(value, high_bits, config_bits, rounding)
    return scaled


def diagnose(request, feature_names, config, groups, spec: FixedSpec,
             high_spec: FixedSpec, threshold_override=None):
    low = run_pipeline_at_spec(request, feature_names, config, groups, spec,
                               threshold_override)
    high = run_pipeline_at_spec(request, feature_names, config, groups, high_spec,
                                threshold_override)
    low_norm, low_fuse, low_rerank = low
    high_norm, high_fuse, high_rerank = high
    tol = config["error_tolerance"]
    item_ids = [c["item_id"] for c in request["candidates"]]
    rounding = spec.rounding
    fb, hfb = spec.frac_bits, high_spec.frac_bits

    stage_diffs = []
    for i, item_id in enumerate(item_ids):
        high_codes = _scale_to_config(high_norm["codes"][i], hfb, fb, rounding)
        norm_diff = [high_codes[j] - low_norm["codes"][i][j]
                     for j in range(len(feature_names))]
        high_sum = _scale_to_config(high_fuse[i]["weighted_sum"], hfb, fb, rounding)
        high_score = _scale_to_config(high_fuse[i]["score"], hfb, fb, rounding)
        fuse_diff = high_sum - low_fuse[i]["weighted_sum"]
        clip_diff = high_score - low_fuse[i]["score"]
        pen_diff = _penalty_diff(low_rerank, high_rerank, item_id, hfb, fb, rounding)
        diffs = {
            "normalization": norm_diff,
            "fusion": fuse_diff,
            "clip": clip_diff,
            "rerank_penalty": pen_diff,
        }
        first_stage = next(
            (stage for stage, value in diffs.items() if _exceeds(value, tol)), None
        )
        stage_diffs.append(
            {
                "item_id": item_id,
                "diffs": diffs,
                "max_abs_norm_diff": max((abs(v) for v in norm_diff), default=0),
                "first_stage_over_tolerance": first_stage,
            }
        )

    low_rank_of = {row["item_id"]: row["rank"] for row in low_rerank["ranking"]}
    high_rank_of = {row["item_id"]: row["rank"] for row in high_rerank["ranking"]}
    boundary_sensitive = []
    for i, item_id in enumerate(item_ids):
        if low_rank_of[item_id] != high_rank_of[item_id]:
            contributions = low_fuse[i]["contributions"]
            dominant_idx = max(range(len(contributions)),
                               key=lambda j: abs(contributions[j]))
            boundary_sensitive.append(
                {
                    "item_id": item_id,
                    "rank_at_config_bits": low_rank_of[item_id],
                    "rank_at_higher_bits": high_rank_of[item_id],
                    "dominant_feature": feature_names[dominant_idx],
                    "dominant_contribution": contributions[dominant_idx],
                }
            )

    rank_diffs = []
    for rank, low_row in enumerate(low_rerank["ranking"], start=1):
        high_row = high_rerank["ranking"][rank - 1]
        if low_row["item_id"] != high_row["item_id"]:
            rank_diffs.append(
                {
                    "rank": rank,
                    "config_bits_item": low_row["item_id"],
                    "higher_bits_item": high_row["item_id"],
                }
            )
    return {
        "config_spec": spec.as_dict(),
        "higher_spec": high_spec.as_dict(),
        "tolerance_lsb": tol,
        "per_candidate_stage_diffs": stage_diffs,
        "boundary_sensitive": boundary_sensitive,
        "rank_diffs_sorted": rank_diffs,
        "first_over_tolerance_summary": _summary(stage_diffs),
    }


def _penalty_diff(low_rerank, high_rerank, item_id, high_bits, config_bits, rounding):
    def total_penalty(rerank):
        return sum(p["deduction"] for p in rerank["penalties"]
                   if p["candidate_item"] == item_id)

    high_total = _scale_to_config(total_penalty(high_rerank), high_bits, config_bits,
                                  rounding)
    return high_total - total_penalty(low_rerank)


def _exceeds(value, tol):
    if isinstance(value, list):
        return any(abs(v) > tol for v in value)
    return abs(value) > tol


def _summary(stage_diffs):
    counts = {}
    for row in stage_diffs:
        stage = row["first_stage_over_tolerance"]
        if stage is not None:
            counts[stage] = counts.get(stage, 0) + 1
    return counts
