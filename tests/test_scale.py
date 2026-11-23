"""业务要求4: 规模变化下精确/近似切换与取舍保证。"""

from alertcorr.correlate import correlate_approx, correlate_exact
from alertcorr.denoise import compute_clusters
from alertcorr.gen import gen_events, gen_topology
from alertcorr.scale import ScaleConfig, decide_modes, estimate_approx_quality
from alertcorr.validation import validate_events_list, validate_topology_dict


def _scaled(n_services=48, n_bursts=16):
    topo_dict = gen_topology(n_services=n_services)
    topo = validate_topology_dict(topo_dict)
    events = validate_events_list(gen_events(topo_dict, n_bursts=n_bursts),
                                  topo)
    return topo, events


def test_mode_switch_by_domain_events(topo_dict, events_list):
    topo = validate_topology_dict(topo_dict)
    events = validate_events_list(events_list, topo)
    cfg = ScaleConfig(exact_max_domain_events=5)
    decisions = decide_modes(topo, events, cfg)
    assert all(d["mode"] == "approx" for d in decisions.values())
    for d in decisions.values():
        assert d["rejected_mode"] == "exact"
        assert "domain_events" in d["rejected_reason"]
        assert d["trigger"]["domain_events"] > 5


def test_mode_switch_by_closure(topo_dict, events_list):
    topo = validate_topology_dict(topo_dict)
    events = validate_events_list(events_list, topo)
    cfg = ScaleConfig(exact_max_closure=1)
    decisions = decide_modes(topo, events, cfg)
    assert all(d["mode"] == "approx" for d in decisions.values())
    cfg2 = ScaleConfig()
    decisions2 = decide_modes(topo, events, cfg2)
    assert all(d["mode"] == "exact" for d in decisions2.values())


def test_approx_quality_estimate_present():
    topo, events = _scaled()
    cfg = ScaleConfig()
    clusters = compute_clusters(events, topo, [], cfg.cluster_window_ms)
    approx, stats = correlate_approx(clusters, topo, cfg.causal_window_ms,
                                     cfg.tolerance,
                                     sample_mod=cfg.approx_sample_mod,
                                     top_k=cfg.top_k)
    quality = estimate_approx_quality(clusters, topo, cfg, approx, stats)
    assert quality["recall_at_k"] is not None
    assert 0.0 <= quality["recall_at_k"] <= 1.0
    assert quality["error_bound"] is not None
    assert stats["skipped"] > 0
    assert stats["skipped_score_upper_bound"] > 0


def test_certified_pairs_never_flip():
    topo, events = _scaled()
    cfg = ScaleConfig()
    clusters = compute_clusters(events, topo, [], cfg.cluster_window_ms)
    exact = correlate_exact(clusters, topo, cfg.causal_window_ms,
                            cfg.tolerance, top_k=cfg.top_k)
    approx, _ = correlate_approx(clusters, topo, cfg.causal_window_ms,
                                 cfg.tolerance,
                                 sample_mod=cfg.approx_sample_mod,
                                 top_k=cfg.top_k)
    flips = 0
    for cid, exact_list in exact.items():
        approx_list = approx.get(cid, [])
        rank = {c["candidate_cluster_id"]: i
                for i, c in enumerate(approx_list)}
        certain = {c["candidate_cluster_id"]:
                   (c["score_exact"] and not c["order_uncertain"])
                   for c in approx_list}
        scores = {c["candidate_cluster_id"]: c["score"] for c in exact_list}
        for i in range(len(exact_list)):
            for j in range(i + 1, len(exact_list)):
                a = exact_list[i]["candidate_cluster_id"]
                b = exact_list[j]["candidate_cluster_id"]
                if a not in rank or b not in rank:
                    continue
                if abs(scores[a] - scores[b]) <= cfg.tolerance:
                    continue
                if not (certain.get(a) and certain.get(b)):
                    continue
                if (rank[a] < rank[b]) != (i < j):
                    flips += 1
    assert flips == 0


def test_uncertain_band_flagged():
    topo, events = _scaled()
    cfg = ScaleConfig()
    clusters = compute_clusters(events, topo, [], cfg.cluster_window_ms)
    approx, _ = correlate_approx(clusters, topo, cfg.causal_window_ms,
                                 cfg.tolerance,
                                 sample_mod=cfg.approx_sample_mod,
                                 top_k=cfg.top_k)
    flagged = sum(1 for cands in approx.values()
                  for c in cands if c["order_uncertain"])
    assert flagged > 0
