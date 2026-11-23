"""根因候选关联与稳定排序, 含精确模式与按依赖距离分桶的近似模式。

评分(纯整数运算, 逐位可复现):
  dist_score   = (MAX_DIST - distance) * W_DIST      因果距离越近分越高
  time_score   = (causal_window_ms - dt) * W_TIME    告警发生越贴近分越高
  weight_score = rep实例权重(微整数) * W_WEIGHT / SCALE
  total        = dist_score + time_score + weight_score
排序键: (-total, distance, dt_ms, candidate_service, candidate_cluster_id)
同分按上述稳定次序裁决; 观测项与聚合顺序写入每条候选的 basis 字段。

近似模式(按依赖距离分桶):
  - 距离 <= 2 的候选精确评分并计算完整证据链;
  - 距离 >= 3 的远桶按服务级确定性探针采样(sha256(service) % sample_mod),
    仅探针服务的候选被精确评分, 非探针远桶候选整体跳过(计入 stats),
    其最大可能得分有解析上界(见 stats["skipped_score_upper_bound"]);
  - 容差带(分差 <= tolerance)内的相邻候选标记 order_uncertain;
  - 因此"已确定的明显胜负"(双方精确且分差 > tolerance)在两种模式间
    不会翻转, 仅容差带内的候选允许顺序变化;
  - 依赖路径只为最终 top-k 候选物化, 非探针远桶不参与枚举,
    这是近似模式相对单流精确嵌套的主要省时来源。
"""

from .model import WEIGHT_SCALE, canonical_json, sha256_text

MAX_DIST_TRACKED = 8
W_DIST = 50_000
W_TIME = 1
W_WEIGHT = 20_000

BUCKET_COUNT = 4


def bucket_of(distance):
    return min(distance, BUCKET_COUNT - 1)


def score_components(distance, dt_ms, weight_micro, causal_window_ms):
    dist_score = (MAX_DIST_TRACKED - min(distance, MAX_DIST_TRACKED)) * W_DIST
    time_score = max(0, causal_window_ms - dt_ms) * W_TIME
    weight_score = (weight_micro * W_WEIGHT) // WEIGHT_SCALE
    return dist_score, time_score, weight_score


def _candidate_payload(cluster, cand, distance, dt_ms, instances,
                       components, score_exact, path):
    dist_score, time_score, weight_score = components
    return {
        "cluster_id": cluster["cluster_id"],
        "rank": 0,
        "candidate_cluster_id": cand["cluster_id"],
        "candidate_service": cand["service"],
        "score": dist_score + time_score + weight_score,
        "score_exact": score_exact,
        "distance": distance,
        "dt_ms": dt_ms,
        "path": path,
        "instances": instances,
        "order_uncertain": False,
        "basis": {
            "observations": ["dependency_distance", "alert_time_delta_ms",
                             "representative_instance_weight"],
            "aggregation": "total = dist_score + time_score + weight_score "
                           "(integer arithmetic, W_DIST=50000, W_TIME=1, "
                           "W_WEIGHT=20000/1e6)",
            "dist_score": dist_score,
            "time_score": time_score,
            "weight_score": weight_score,
            "tie_break": ["distance", "dt_ms", "candidate_service",
                          "candidate_cluster_id"],
        },
    }


def _sort_key(cand):
    return (-cand["score"], cand["distance"], cand["dt_ms"],
            cand["candidate_service"], cand["candidate_cluster_id"])


def _seal(payload):
    payload["sha"] = sha256_text(canonical_json(payload))
    return payload


def _build(cluster, raw_cands, topology, causal_window_ms, score_exact):
    built = []
    for cand, distance, dt_ms in raw_cands:
        weight = topology.instances[cand["rep_instance"]].weight_micro
        components = score_components(distance, dt_ms, weight,
                                      causal_window_ms)
        instances = sorted({cand["rep_instance"], cluster["rep_instance"]})
        built.append(_candidate_payload(cluster, cand, distance, dt_ms,
                                        instances, components,
                                        score_exact, path=[]))
    return built


def _materialize_paths(sorted_cands, cluster, topology, path_cache, top_k):
    """只为 top-k 候选物化依赖路径证据链, 其余标记 path_deferred。"""
    for rank, cand in enumerate(sorted_cands):
        if rank < top_k:
            key = (cand["candidate_service"], cluster["service"])
            if key not in path_cache:
                path_cache[key] = topology.dependency_path(key[0], key[1])
            cand["path"] = path_cache[key]
        else:
            cand["basis"]["path_deferred"] = True
        cand["rank"] = rank
    return sorted_cands


def _mark_tolerance_band(sorted_cands, tolerance):
    """容差带: 相邻候选分差 <= tolerance 时两者都标记 order_uncertain。"""
    for i in range(len(sorted_cands) - 1):
        if abs(sorted_cands[i]["score"]
               - sorted_cands[i + 1]["score"]) <= tolerance:
            sorted_cands[i]["order_uncertain"] = True
            sorted_cands[i + 1]["order_uncertain"] = True
    return sorted_cands


def _self_root(cluster, topology, causal_window_ms):
    """闭包内无其他候选时, 该簇自身即根因(距离0, 时间差0)。"""
    weight = topology.instances[cluster["rep_instance"]].weight_micro
    components = score_components(0, 0, weight, causal_window_ms)
    payload = _candidate_payload(
        cluster, cluster, 0, 0, [cluster["rep_instance"]],
        components, score_exact=True, path=[cluster["service"]])
    payload["basis"]["self_root"] = True
    return [_seal(payload)]


def correlate_exact(clusters, topology, causal_window_ms, tolerance,
                    cluster_ids=None, top_k=5):
    """单流精确嵌套关联: 每个簇枚举上游闭包内全部活跃簇并精确评分。"""
    active = {cid: c for cid, c in clusters.items() if c["status"] == "active"}
    closure_cache = {name: topology.upstream_closure(name)
                     for name in topology.services}
    path_cache = {}
    targets = sorted(active) if cluster_ids is None else sorted(cluster_ids)
    result = {}
    for cid in targets:
        if cid not in active:
            continue
        cluster = active[cid]
        closure = closure_cache[cluster["service"]]
        raw = []
        for cand_id, cand in active.items():
            if cand_id == cid:
                continue
            if cand["service"] not in closure:
                continue
            dt = cluster["rep_ts"] - cand["rep_ts"]
            if dt < 0 or dt > causal_window_ms:
                continue
            raw.append((cand, closure[cand["service"]], dt))
        if not raw:
            result[cid] = _self_root(cluster, topology, causal_window_ms)
            continue
        built = _build(cluster, raw, topology, causal_window_ms,
                       score_exact=True)
        ordered = sorted(built, key=_sort_key)
        ordered = _mark_tolerance_band(ordered, tolerance)
        ordered = _materialize_paths(ordered, cluster, topology,
                                     path_cache, top_k=len(ordered))
        result[cid] = [_seal(c) for c in ordered]
    return result


def correlate_approx(clusters, topology, causal_window_ms, tolerance,
                     sample_mod=4, cluster_ids=None, top_k=5):
    """按依赖距离分桶的近似关联。返回 (candidates, stats)。"""
    active = {cid: c for cid, c in clusters.items() if c["status"] == "active"}
    by_service = {}
    for cid, c in active.items():
        by_service.setdefault(c["service"], []).append((cid, c))
    closure_cache = {name: topology.upstream_closure(name)
                     for name in topology.services}
    probe_services = {name for name in topology.services
                      if int(sha256_text(name)[:8], 16) % sample_mod == 0}
    max_weight = max(inst.weight_micro
                     for inst in topology.instances.values())
    skipped_upper = (score_components(3, 0, max_weight, causal_window_ms))
    skipped_upper = sum(skipped_upper)
    path_cache = {}
    targets = sorted(active) if cluster_ids is None else sorted(cluster_ids)
    result = {}
    skipped = 0
    exact = 0
    for cid in targets:
        if cid not in active:
            continue
        cluster = active[cid]
        closure = closure_cache[cluster["service"]]
        raw = []
        for svc in sorted(closure):
            distance = closure[svc]
            if svc not in by_service:
                continue
            if distance > 2 and svc not in probe_services:
                skipped += sum(1 for cand_id, _ in by_service[svc]
                               if cand_id != cid)
                continue
            for cand_id, cand in by_service[svc]:
                if cand_id == cid:
                    continue
                dt = cluster["rep_ts"] - cand["rep_ts"]
                if dt < 0 or dt > causal_window_ms:
                    continue
                raw.append((cand, distance, dt))
        exact += len(raw)
        if not raw:
            result[cid] = _self_root(cluster, topology, causal_window_ms)
            continue
        built = _build(cluster, raw, topology, causal_window_ms,
                       score_exact=True)
        ordered = sorted(built, key=_sort_key)
        ordered = _mark_tolerance_band(ordered, tolerance)
        ordered = _materialize_paths(ordered, cluster, topology,
                                     path_cache, top_k=top_k)
        result[cid] = [_seal(c) for c in ordered]
    stats = {"exact_scored": exact, "skipped": skipped,
             "sample_mod": sample_mod,
             "skipped_score_upper_bound": skipped_upper,
             "skipped_upper_bound_definition":
                 "dist_score(d=3) + time_score(dt=0) + "
                 "weight_score(max_instance_weight)"}
    return result, stats
