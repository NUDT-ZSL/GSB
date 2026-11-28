"""Root-cause candidate scoring and downstream impact propagation."""

PROXIMITY = {0: 0.5, 1: 0.9, 2: 0.7, 3: 0.55}
DEFAULT_PROXIMITY = 0.4
EXPLAINED_PENALTY = 0.5


def _overlap(a_start, a_end, b_start, b_end):
    return max(0, min(a_end, b_end) - max(a_start, b_start) + 1)


def _mode_match(a, b):
    return 1.0 if a == b else 0.3


def attribute_events(events, catalog, strategy):
    """Attach ranked root-cause candidates and impacted services to each event."""
    weights = strategy["scoring_weights"]
    for event in events:
        svc = event["service"]
        upstream = catalog.ancestors(svc)
        candidates = [e for e in events
                      if e["service"] == svc or e["service"] in upstream]
        event_len = event["end_tick"] - event["start_tick"] + 1
        scored = []
        for cand in candidates:
            ov = _overlap(event["start_tick"], event["end_tick"],
                          cand["start_tick"], cand["end_tick"])
            if ov <= 0:
                continue
            overlap_ratio = ov / event_len
            if cand["service"] == svc:
                dist = 0
            else:
                dist = catalog.distance(svc, cand["service"])
            proximity = PROXIMITY.get(dist, DEFAULT_PROXIMITY)
            match = _mode_match(event["dominant_failure_mode"],
                                cand["dominant_failure_mode"])
            base = (weights["time_overlap"] * overlap_ratio
                    + weights["dependency_proximity"] * proximity
                    + weights["failure_mode_match"] * match)
            penalty = 0.0
            for other in candidates:
                if other is cand or other["service"] == cand["service"]:
                    continue
                if other["service"] not in catalog.ancestors(cand["service"]):
                    continue
                other_ov = _overlap(cand["start_tick"], cand["end_tick"],
                                    other["start_tick"], other["end_tick"])
                if other_ov <= 0:
                    continue
                cand_len = cand["end_tick"] - cand["start_tick"] + 1
                ratio = other_ov / cand_len
                m = _mode_match(cand["dominant_failure_mode"],
                                other["dominant_failure_mode"])
                penalty = max(penalty, ratio * m)
            score = round(base * (1.0 - EXPLAINED_PENALTY * penalty), 6)
            scored.append({
                "event_id": cand["event_id"],
                "service": cand["service"],
                "interface": cand["interface"],
                "score": score,
                "time_overlap_ratio": round(overlap_ratio, 6),
                "dependency_distance": dist,
                "failure_mode_match": match,
                "evidence": {
                    "start_tick": cand["start_tick"],
                    "end_tick": cand["end_tick"],
                    "interfaces": [cand["interface"]],
                },
            })
        scored.sort(key=lambda c: (-c["score"], c["service"], c["interface"]))
        event["root_cause_candidates"] = scored

        if scored:
            root_service = scored[0]["service"]
        else:
            root_service = svc
        impacted_direct = []
        impacted_indirect = []
        for dep, dist in sorted(catalog.dependents_with_distance(root_service).items()):
            entry = {"service": dep,
                     "interfaces": sorted(catalog.services[dep]["interfaces"])}
            if dist == 1:
                impacted_direct.append(entry)
            else:
                impacted_indirect.append(entry)
        event["impacted"] = {"direct": impacted_direct, "indirect": impacted_indirect}
    return events
