"""单请求四阶段流水线：加载 -> 归一化 -> 融合 -> 重排（含误差诊断）。"""


from . import injection as inj
from .cache import fusion_key, normalization_key, rerank_key
from .diagnostics import diagnose
from .fingerprint import stable_key
from .fixedpoint import FixedSpec
from .fusion import fuse_request
from .normalize import normalize_request
from .rerank import rerank_request


def prepare_candidates(request, feature_names, recall_names, dense_names, sparse_names):
    """把 JSONL 候选记录展开为固定顺序的完整特征向量可访问结构。"""
    prepared = {"request_id": request["request_id"], "candidates": []}
    for cand in request["candidates"]:
        dense_map = {name: cand["dense"][i] for i, name in enumerate(dense_names)}
        record = {
            "item_id": cand["item_id"],
            "scores": dict(cand["scores"]),
            "dense_map": dense_map,
            "sparse": {name: cand["sparse"].get(name, 0) for name in sparse_names},
            "line_no": cand["line_no"],
        }
        prepared["candidates"].append(record)
    return prepared


def run_request(request_raw, config, groups_resolved, groups_hash_value, spec,
                injection_items, cache_book, existing_artifact):
    """执行一个请求，返回 (请求结果, 阶段版本字典)。

    existing_artifact 为快照中可复用的上一轮产物；缓存键不变则直接复用，
    键变化的阶段标记 invalidated 并重算，下游级联失效。
    """
    request = prepare_candidates(
        request_raw,
        config["feature_names"],
        [n for n in config["feature_names"] if n.startswith("recall_")],
        [n for n in config["feature_names"] if n.startswith("dense_")],
        [n for n in config["feature_names"] if n.startswith("sparse_")],
    )
    rid = request["request_id"]
    injection_token = sorted(item["id"] for item in injection_items)
    threshold_override, threshold_hits = inj.rerank_threshold_override(injection_items)
    stage_versions = {"load": 1, "normalize": 0, "fuse": 0, "rerank": 0}

    cached = existing_artifact or {}
    cached_keys = cached.get("cache_keys", {})

    key_norm = normalization_key(request, config, spec.as_dict(), injection_token)
    if cached_keys.get("normalize") == key_norm and cached.get("norm"):
        cache_book.record(rid, "normalize", "hit", key_norm)
        norm_result = _decode_norm(cached["norm"])
        stage_versions["normalize"] = cached["stage_versions"]["normalize"]
    else:
        already_invalidated = any(
            e["stage"] == "normalize" and e["status"] == "invalidated"
            for e in cache_book.for_request(rid)
        )
        if cached and not already_invalidated:
            cache_book.record(rid, "normalize", "invalidated", key_norm)
        inj.apply_normalize_injection(request, injection_items, config["feature_names"])
        norm_result = normalize_request(
            request, config["feature_names"], config["normalization"], spec
        )
        cache_book.record(rid, "normalize", "recompute" if cached else "miss", key_norm)
        stage_versions["normalize"] = cached.get("stage_versions", {}).get("normalize", 0) + 1

    key_fuse = fusion_key(norm_result, config, spec.as_dict(), injection_token)
    if cached_keys.get("fuse") == key_fuse and cached.get("fused"):
        cache_book.record(rid, "fuse", "hit", key_fuse)
        fused = cached["fused"]
        stage_versions["fuse"] = cached["stage_versions"]["fuse"]
    else:
        if cached and cached_keys.get("normalize") == key_norm:
            cache_book.record(rid, "fuse", "invalidated", key_fuse)
        fuse_config = dict(config)
        inj.apply_fuse_config_injection(fuse_config, injection_items)
        fused = fuse_request(
            norm_result, config["weights"], config["temperature"],
            config["score_clip"], spec,
        )
        cache_book.record(rid, "fuse", "recompute" if cached else "miss", key_fuse)
        stage_versions["fuse"] = (cached.get("stage_versions", {}).get("fuse", 0) + 1)

    key_rerank = rerank_key(
        fused, norm_result, config, groups_hash_value, spec.as_dict(),
        threshold_override, injection_token,
    )
    if cached_keys.get("rerank") == key_rerank and cached.get("rerank"):
        cache_book.record(rid, "rerank", "hit", key_rerank)
        rerank_result = cached["rerank"]
        stage_versions["rerank"] = cached["stage_versions"]["rerank"]
    else:
        if cached and cached_keys.get("fuse") == key_fuse:
            cache_book.record(rid, "rerank", "invalidated", key_rerank)
        item_ids = [c["item_id"] for c in request["candidates"]]
        scores = [f["score"] for f in fused]
        rerank_result = rerank_request(
            item_ids, norm_result["codes"], scores, groups_resolved,
            config, spec, threshold_override=threshold_override,
        )
        cache_book.record(rid, "rerank", "recompute" if cached else "miss", key_rerank)
        stage_versions["rerank"] = (
            cached.get("stage_versions", {}).get("rerank", 0) + 1
        )

    high_spec = FixedSpec(spec.frac_bits + 1, spec.levels, spec.rounding)
    diagnostics = diagnose(
        request, config["feature_names"], config, groups_resolved,
        spec, high_spec, threshold_override=threshold_override,
    )

    artifact = {
        "request_id": rid,
        "item_ids": [c["item_id"] for c in request["candidates"]],
        "stable_keys": [stable_key(rid, c["item_id"]) for c in request["candidates"]],
        "stage_versions": stage_versions,
        "cache_keys": {
            "normalize": key_norm,
            "fuse": key_fuse,
            "rerank": key_rerank,
        },
        "norm": _encode_norm(norm_result),
        "fused": fused,
        "rerank": rerank_result,
        "diagnostics": diagnostics,
        "injection_hits": sorted(
            set(threshold_hits) | {i["id"] for i in injection_items if i["id"] in injection_token}
        ),
        "residuals": {
            "convert_residuals": norm_result["convert_residuals"],
            "quant_residuals": norm_result["quant_residuals"],
            "clip_deltas": [f["clip_delta"] for f in fused],
            "scale_remainders": [f["scale_remainder"] for f in fused],
        },
    }
    return artifact, stage_versions


def _encode_norm(norm_result):
    return {
        "spec": norm_result["spec"],
        "guard_bits": norm_result["guard_bits"],
        "stats": norm_result["stats"],
        "codes": norm_result["codes"],
        "convert_residuals": norm_result["convert_residuals"],
        "quant_residuals": norm_result["quant_residuals"],
    }


def _decode_norm(encoded):
    return {
        "spec": encoded["spec"],
        "guard_bits": encoded["guard_bits"],
        "stats": encoded["stats"],
        "codes": encoded["codes"],
        "convert_residuals": encoded.get("convert_residuals", []),
        "quant_residuals": encoded.get("quant_residuals", []),
        "raw_matrix": [],
    }
