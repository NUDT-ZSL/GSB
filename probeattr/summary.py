"""Per-team availability summary built from the same event objects."""


def _severity(event, strategy):
    if event["type"] == "latency_degradation":
        return round(event["max_p95_ms"] / strategy["latency_threshold_ms"], 3)
    return event["max_failure_rate"]


def build_team_summary(events, catalog, strategy):
    teams = {}
    for sid, spec in catalog.services.items():
        teams.setdefault(spec["team"], {"services": set(), "events": []})
        teams[spec["team"]]["services"].add(sid)
    for event in events:
        team = catalog.services[event["service"]]["team"]
        teams[team]["events"].append(event)

    summary = []
    for team in sorted(teams):
        entry = teams[team]
        mode_counts = {}
        pending = []
        for event in entry["events"]:
            mode = event["dominant_failure_mode"]
            mode_counts[mode] = mode_counts.get(mode, 0) + 1
            impacted = event.get("impacted", {"direct": [], "indirect": []})
            blast = len(impacted["direct"]) + len(impacted["indirect"])
            duration = event["end_tick"] - event["start_tick"] + 1
            priority = round(duration * _severity(event, strategy) * (1 + blast), 6)
            pending.append({
                "event_id": event["event_id"],
                "service": event["service"],
                "interface": event["interface"],
                "type": event["type"],
                "start_tick": event["start_tick"],
                "end_tick": event["end_tick"],
                "priority_score": priority,
            })
        pending.sort(key=lambda p: (-p["priority_score"], p["event_id"]))
        summary.append({
            "team": team,
            "services": sorted(entry["services"]),
            "event_count": len(entry["events"]),
            "dominant_failure_modes": [
                {"mode": mode, "count": mode_counts[mode]}
                for mode in sorted(mode_counts, key=lambda m: (-mode_counts[m], m))
            ],
            "pending_events": pending,
        })
    return summary
