#!/usr/bin/env python3
"""精确 vs 近似关联的可复现比较。

在确定性规模样本上分别强制两种模式, 输出:
  - recall@k (近似 top-k 与精确 top-k 交集比例)
  - 容差带外的顺序翻转数 (必须为 0, 即明显胜负不得翻转)
  - 容差带内被标记的顺序不确定对数
  - 关联阶段耗时与 numpy 汇总统计
用法: python3 scripts/compare_modes.py
"""

import json
import os
import sys
import time

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from alertcorr.correlate import correlate_approx, correlate_exact
from alertcorr.denoise import compute_clusters
from alertcorr.gen import gen_events, gen_topology
from alertcorr.scale import ScaleConfig
from alertcorr.validation import validate_events_list, validate_topology_dict


def compare_at_scale(n_services, n_bursts, cfg):
    topo_dict = gen_topology(n_services=n_services)
    topo = validate_topology_dict(topo_dict)
    events = validate_events_list(gen_events(topo_dict, n_bursts=n_bursts),
                                  topo)
    clusters = compute_clusters(events, topo, [], cfg.cluster_window_ms)

    t0 = time.perf_counter()
    exact = correlate_exact(clusters, topo, cfg.causal_window_ms,
                            cfg.tolerance, top_k=cfg.top_k)
    t_exact = time.perf_counter() - t0

    t0 = time.perf_counter()
    approx, stats = correlate_approx(clusters, topo, cfg.causal_window_ms,
                                     cfg.tolerance,
                                     sample_mod=cfg.approx_sample_mod,
                                     top_k=cfg.top_k)
    t_approx = time.perf_counter() - t0

    hits = 0
    total = 0
    flips_outside_tolerance = 0
    uncertain_pairs = 0
    for cid in exact:
        exact_list = exact[cid]
        approx_list = approx.get(cid, [])
        exact_top = [c["candidate_cluster_id"]
                     for c in exact_list[:cfg.top_k]]
        approx_top = [c["candidate_cluster_id"]
                      for c in approx_list[:cfg.top_k]]
        hits += len(set(exact_top) & set(approx_top))
        total += len(exact_top)
        approx_rank = {c["candidate_cluster_id"]: i
                       for i, c in enumerate(approx_list)}
        approx_certain = {c["candidate_cluster_id"]:
                          (c["score_exact"] and not c["order_uncertain"])
                          for c in approx_list}
        exact_score = {c["candidate_cluster_id"]: c["score"]
                       for c in exact_list}
        for i in range(len(exact_list)):
            for j in range(i + 1, len(exact_list)):
                a = exact_list[i]["candidate_cluster_id"]
                b = exact_list[j]["candidate_cluster_id"]
                if a not in approx_rank or b not in approx_rank:
                    continue
                gap = abs(exact_score[a] - exact_score[b])
                if gap <= cfg.tolerance:
                    uncertain_pairs += 1
                    continue
                certified = (approx_certain.get(a, False)
                             and approx_certain.get(b, False))
                if not certified:
                    continue
                exact_order = i < j
                approx_order = approx_rank[a] < approx_rank[b]
                if exact_order != approx_order:
                    flips_outside_tolerance += 1
    return {
        "n_services": n_services,
        "n_events": len(events),
        "n_clusters": len(clusters),
        "recall_at_k": hits / total if total else None,
        "flips_outside_tolerance": flips_outside_tolerance,
        "uncertain_pairs_flagged": uncertain_pairs,
        "approx_skipped_scores": stats["skipped"],
        "approx_exact_scores": stats["exact_scored"],
        "t_exact_ms": t_exact * 1000,
        "t_approx_ms": t_approx * 1000,
    }


def main():
    cfg = ScaleConfig()
    scales = [(24, 8), (48, 16), (96, 32)]
    rows = [compare_at_scale(n, b, cfg) for n, b in scales]
    for row in rows:
        print("services=%d events=%d clusters=%d recall@%d=%.4f "
              "flips_outside_tol=%d exact_ms=%.1f approx_ms=%.1f"
              % (row["n_services"], row["n_events"], row["n_clusters"],
                 cfg.top_k, row["recall_at_k"] or 0.0,
                 row["flips_outside_tolerance"], row["t_exact_ms"],
                 row["t_approx_ms"]))
    summary = {
        "config": dict(cfg.__dict__),
        "runs": rows,
        "recall_mean": float(np.mean([r["recall_at_k"] for r in rows])),
        "recall_std": float(np.std([r["recall_at_k"] for r in rows])),
        "speedup_approx_vs_exact": float(
            np.mean([r["t_exact_ms"] / max(r["t_approx_ms"], 1e-9)
                     for r in rows])),
        "total_flips_outside_tolerance": int(
            sum(r["flips_outside_tolerance"] for r in rows)),
    }
    os.makedirs("out", exist_ok=True)
    with open(os.path.join("out", "mode_comparison.json"), "w",
              encoding="utf-8") as f:
        json.dump(summary, f, ensure_ascii=False, indent=2, sort_keys=True)
    print("recall mean=%.4f std=%.4f speedup=%.2fx flips_outside_tol=%d"
          % (summary["recall_mean"], summary["recall_std"],
             summary["speedup_approx_vs_exact"],
             summary["total_flips_outside_tolerance"]))
    print("written: out/mode_comparison.json")
    return 0


if __name__ == "__main__":
    sys.exit(main())
