"""Deterministic synthetic probe record and ground-truth generator."""

import random

BASE_NOISE_PROB = 0.004
TIMEOUT_BUDGET_MS = 3000
PROPAGATION_PROB = {1: 0.75, 2: 0.70, 3: 0.65}
NOISE_MODES = ("timeout", "http_5xx", "conn_refused", "rate_limited")
LATENCY_FAULT_MIN_MS = 1500.0


def build_catalog():
    services = [
        ("svc-gateway", "接入网关", "team-platform",
         ["gw.dispatch", "gw.session"], ["svc-acceptance", "svc-license-verify"]),
        ("svc-acceptance", "办件受理", "team-accept",
         ["accept.submit", "accept.status", "accept.cancel"],
         ["svc-identity", "svc-license-issue", "svc-archive", "svc-notify"]),
        ("svc-license-issue", "电子证照签发", "team-license",
         ["issue.apply", "issue.sign"], ["svc-identity", "svc-archive"]),
        ("svc-license-verify", "证照核验", "team-license",
         ["verify.check", "verify.batch"], ["svc-license-issue", "svc-identity"]),
        ("svc-identity", "身份认证", "team-platform",
         ["idn.token", "idn.refresh"], []),
        ("svc-archive", "档案存储", "team-infra",
         ["arch.put", "arch.get"], []),
        ("svc-notify", "消息通知", "team-infra",
         ["notify.send"], []),
    ]
    return {
        "catalog_version": "v1",
        "services": [
            {"service_id": sid, "name": name, "team": team,
             "interfaces": interfaces, "depends_on": depends_on}
            for sid, name, team, interfaces, depends_on in services
        ],
    }


def build_strategy():
    return {
        "parameter_version": "v1.0.0",
        "probe_period_seconds": 60,
        "tick_count": 240,
        "node_count": 3,
        "window_length": 12,
        "consecutive_failure_threshold": 3,
        "latency_consecutive_threshold": 3,
        "failure_rate_threshold": 0.5,
        "latency_percentile": "p95",
        "latency_threshold_ms": 1000,
        "scoring_weights": {
            "time_overlap": 0.45,
            "dependency_proximity": 0.35,
            "failure_mode_match": 0.2,
        },
    }


def plan_faults(rng):
    f1_start = 30 + rng.randint(-3, 3)
    f2_start = 95 + rng.randint(-3, 3)
    f3_start = 155 + rng.randint(-3, 3)
    return [
        {"fault_id": "F1", "type": "availability", "service": "svc-archive",
         "interface": "arch.get", "start_tick": f1_start,
         "end_tick": f1_start + 16 + rng.randint(0, 4),
         "failure_mode": "timeout", "probability": 0.9},
        {"fault_id": "F2", "type": "availability", "service": "svc-license-issue",
         "interface": "issue.sign", "start_tick": f2_start,
         "end_tick": f2_start + 28 + rng.randint(0, 4),
         "failure_mode": "http_5xx", "probability": 0.85},
        {"fault_id": "F3", "type": "latency", "service": "svc-identity",
         "interface": "idn.token", "start_tick": f3_start,
         "end_tick": f3_start + 22 + rng.randint(0, 4),
         "latency_multiplier": 20},
    ]


def _dependents_distances(catalog, service):
    dependents = {s["service_id"]: set() for s in catalog["services"]}
    for spec in catalog["services"]:
        for dep in spec["depends_on"]:
            dependents[dep].add(spec["service_id"])
    dist = {}
    frontier = {service}
    hops = 0
    while frontier:
        hops += 1
        nxt = set()
        for cur in frontier:
            for dep in sorted(dependents[cur]):
                if dep not in dist and dep != service:
                    dist[dep] = hops
                    nxt.add(dep)
        frontier = nxt
    return dist


def generate(seed):
    """Return (catalog, strategy, records, ground_truth) deterministically."""
    catalog = build_catalog()
    strategy = build_strategy()
    rng = random.Random(seed)
    faults = plan_faults(rng)

    services = catalog["services"]
    base_latency = {}
    for spec in services:
        for iface in spec["interfaces"]:
            sub = random.Random(f"{seed}:latency:{iface}")
            base_latency[iface] = sub.uniform(30.0, 180.0)

    fault_distances = {f["fault_id"]: _dependents_distances(catalog, f["service"])
                       for f in faults}

    records = []
    tick_count = strategy["tick_count"]
    nodes = [f"node-{i + 1}" for i in range(strategy["node_count"])]
    for tick in range(tick_count):
        active = [f for f in faults if f["start_tick"] <= tick <= f["end_tick"]]
        for spec in services:
            sid = spec["service_id"]
            for iface in spec["interfaces"]:
                contribs = []
                latency_mult = 1.0
                for fault in active:
                    if fault["type"] == "latency":
                        if fault["service"] == sid and fault["interface"] == iface:
                            latency_mult *= fault["latency_multiplier"]
                    else:
                        if fault["service"] == sid and fault["interface"] == iface:
                            contribs.append((fault["probability"], fault["failure_mode"]))
                        dist = fault_distances[fault["fault_id"]].get(sid)
                        if dist is not None and dist in PROPAGATION_PROB:
                            contribs.append((PROPAGATION_PROB[dist], fault["failure_mode"]))
                survival = 1.0 - BASE_NOISE_PROB
                for prob, _mode in contribs:
                    survival *= 1.0 - prob
                fail_prob = 1.0 - survival
                fault_mode = max(contribs)[1] if contribs else None
                base = base_latency[iface]
                for node in nodes:
                    failed = rng.random() < fail_prob
                    if failed:
                        mode = fault_mode if fault_mode else rng.choice(NOISE_MODES)
                    else:
                        mode = "none"
                    if latency_mult > 1.0:
                        latency = max(base * latency_mult, LATENCY_FAULT_MIN_MS) \
                            * rng.uniform(0.9, 1.1)
                    else:
                        latency = base * rng.uniform(0.7, 1.3)
                    if mode == "none":
                        status = 200
                    elif mode == "timeout":
                        status = 504
                        latency = TIMEOUT_BUDGET_MS + rng.uniform(0.0, 200.0)
                    elif mode == "http_5xx":
                        status = rng.choice((500, 502, 503))
                        latency = base * rng.uniform(0.2, 0.6)
                    elif mode == "conn_refused":
                        status = 0
                        latency = rng.uniform(2.0, 8.0)
                    else:
                        status = 429
                        latency = base * rng.uniform(0.1, 0.3)
                    records.append({
                        "tick": tick,
                        "service": sid,
                        "interface": iface,
                        "node": node,
                        "status": status,
                        "latency_ms": max(1, int(round(latency))),
                        "failure_reason": mode,
                    })

    ground_truth = {"seed": seed, "parameter_version": strategy["parameter_version"],
                    "faults": []}
    for fault in faults:
        expected = [{
            "service": fault["service"],
            "interface": fault["interface"],
            "kind": "self",
            "distance": 0,
            "event_type": ("latency_degradation" if fault["type"] == "latency"
                           else "sustained_failure"),
        }]
        if fault["type"] == "availability":
            for sid, dist in sorted(fault_distances[fault["fault_id"]].items()):
                if dist not in PROPAGATION_PROB:
                    continue
                for spec in services:
                    if spec["service_id"] != sid:
                        continue
                    for iface in spec["interfaces"]:
                        expected.append({
                            "service": sid,
                            "interface": iface,
                            "kind": "propagated",
                            "distance": dist,
                            "event_type": "sustained_failure",
                        })
        entry = dict(fault)
        entry["expected_events"] = expected
        ground_truth["faults"].append(entry)
    return catalog, strategy, records, ground_truth
