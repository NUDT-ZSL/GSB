"""确定性规模样本生成器: 全部随机性来自 hashlib, 任意次运行逐位一致。"""

import hashlib


def _rand_int(seed, mod):
    return int(hashlib.sha256(seed.encode("utf-8")).hexdigest()[:12], 16) % mod


def gen_topology(n_services=24, n_domains=3, layer_size=6):
    services = []
    layers = []
    idx = 0
    while idx < n_services:
        layer = []
        for _ in range(min(layer_size, n_services - idx)):
            layer.append("svc-%03d" % idx)
            idx += 1
        layers.append(layer)
    for li, layer in enumerate(layers):
        for name in layer:
            upstream = []
            if li > 0:
                prev = layers[li - 1]
                n_deps = 1 + _rand_int("dep|%s" % name, min(3, len(prev)))
                picks = sorted({_rand_int("dep|%s|%d" % (name, k), len(prev))
                                for k in range(n_deps)})
                upstream = [prev[p] for p in picks]
            n_inst = 1 + _rand_int("inst|%s" % name, 3)
            instances = [
                {"id": "%s-i%d" % (name, j),
                 "weight": (5 + _rand_int("w|%s|%d" % (name, j), 26)) / 10.0}
                for j in range(n_inst)]
            services.append({
                "name": name,
                "fault_domain": "fd-%d" % (_rand_int("dom|%s" % name,
                                                     n_domains)),
                "upstream": upstream,
                "instances": instances,
            })
    return {"version": 1, "services": services}


def gen_events(topo_dict, n_bursts=8, seq_start=1):
    """沿依赖链生成确定性告警突发: 根因服务先告警, 下游依次跟进。"""
    from .validation import validate_topology_dict
    topo = validate_topology_dict(topo_dict)
    down = topo.downstream_map()
    rules = ["cpu_high", "mem_high", "latency_high", "error_rate", "http_5xx"]
    sevs = ["warning", "major", "critical"]
    events = []
    seq = seq_start
    for burst in range(n_bursts):
        root_idx = _rand_int("burst|%d" % burst, len(topo.services))
        root = sorted(topo.services)[root_idx]
        base_ts = _rand_int("ts|%d" % burst, 50000) + burst * 120000
        chain = {root: 0}
        frontier = [root]
        while frontier:
            nxt = []
            for node in frontier:
                for child in sorted(down[node]):
                    if child not in chain:
                        chain[child] = chain[node] + 1
                        nxt.append(child)
            frontier = nxt
        for svc in sorted(chain, key=lambda s: (chain[s], s)):
            svc_obj = topo.services[svc]
            n_alerts = 1 + _rand_int("na|%d|%s" % (burst, svc), 3)
            for k in range(n_alerts):
                inst = svc_obj.instances[
                    _rand_int("ii|%d|%s|%d" % (burst, svc, k),
                              len(svc_obj.instances))]
                rule = rules[_rand_int("rl|%s|%d" % (svc, k), len(rules))]
                sev = sevs[_rand_int("sv|%d|%s|%d" % (burst, svc, k),
                                     len(sevs))]
                ts = base_ts + chain[svc] * 2000 + k * 300
                flag = "none"
                if _rand_int("fl|%d|%s|%d" % (burst, svc, k), 17) == 0:
                    flag = "silenced"
                events.append({
                    "seq": seq, "instance_id": inst, "rule": rule,
                    "severity": sev, "logical_ts_ms": ts,
                    "metric": _rand_int("mt|%d" % seq, 1000) / 10.0,
                    "flag": flag})
                seq += 1
    events.sort(key=lambda e: e["seq"])
    return events
