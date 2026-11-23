"""业务要求3: 根因候选关联、稳定排序与判定依据。"""

from alertcorr.correlate import correlate_exact
from alertcorr.denoise import compute_clusters
from alertcorr.validation import validate_events_list, validate_topology_dict

CFG = dict(cluster_window_ms=60_000, causal_window_ms=300_000, tolerance=5_000)


def _setup(topo_dict, events_list):
    topo = validate_topology_dict(topo_dict)
    events = validate_events_list(events_list, topo)
    clusters = compute_clusters(events, topo, [], CFG["cluster_window_ms"])
    result = correlate_exact(clusters, topo, CFG["causal_window_ms"],
                             CFG["tolerance"])
    return clusters, result


def test_upstream_root_cause_ranked_first(topo_dict, events_list):
    _, result = _setup(topo_dict, events_list)
    top = result["api~latency_high~0000"][0]
    assert top["candidate_service"] == "db"
    assert top["distance"] == 1
    assert top["dt_ms"] > 0
    assert top["path"] == ["db", "api"]


def test_candidate_payload_fields(topo_dict, events_list):
    _, result = _setup(topo_dict, events_list)
    for cands in result.values():
        for cand in cands:
            assert cand["instances"]
            assert cand["path"]
            assert cand["dt_ms"] >= 0
            basis = cand["basis"]
            assert basis["observations"] == [
                "dependency_distance", "alert_time_delta_ms",
                "representative_instance_weight"]
            assert basis["tie_break"] == ["distance", "dt_ms",
                                          "candidate_service",
                                          "candidate_cluster_id"]
            assert (cand["score"] == basis["dist_score"]
                    + basis["time_score"] + basis["weight_score"])


def test_stable_tie_break(topo_dict, events_list):
    _, result = _setup(topo_dict, events_list)
    for cands in result.values():
        keys = [(-c["score"], c["distance"], c["dt_ms"],
                 c["candidate_service"], c["candidate_cluster_id"])
                for c in cands]
        assert keys == sorted(keys)
        assert [c["rank"] for c in cands] == list(range(len(cands)))


def test_time_ordering_required(topo_dict, events_list):
    clusters, result = _setup(topo_dict, events_list)
    for cid, cands in result.items():
        rep_ts = clusters[cid]["rep_ts"]
        for cand in cands:
            cand_ts = clusters[cand["candidate_cluster_id"]]["rep_ts"]
            assert 0 <= rep_ts - cand_ts <= CFG["causal_window_ms"]


def test_self_root_fallback(topo_dict, events_list):
    _, result = _setup(topo_dict, events_list)
    roots = [c[0] for c in result.values()
             if c[0]["basis"].get("self_root")]
    assert roots
    for root in roots:
        assert root["distance"] == 0
        assert root["dt_ms"] == 0
