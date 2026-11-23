"""Deterministic parsing and strict validation of every input artifact.

Validation is read only: on failure nothing is written to SQLite or the
working directory.  Each rule violation surfaces a stable business code via
:class:`~scaler.errors.ScalerError`.
"""

import json
import sqlite3
from decimal import Decimal

from .errors import bad_sample, bad_shard
from .models import (
    Service,
    Node,
    V1Policy,
    Gear,
    V2Shard,
    V2Gear,
)


def _require(condition, message, **details):
    if not condition:
        raise bad_sample(message, **details)


def _is_int(value):
    return isinstance(value, int) and not isinstance(value, bool)


def load_json(path):
    with open(path, "r", encoding="utf-8") as handle:
        return json.load(handle)


# --------------------------------------------------------------------- services
def parse_services(payload):
    _require(isinstance(payload, list) and payload, "services must be a non empty list")
    services = []
    seen = set()
    names = set()
    for index, row in enumerate(payload):
        _require(isinstance(row, dict), "service entry must be an object", index=index)
        name = row.get("name")
        _require(isinstance(name, str) and name, "service name missing", index=index)
        _require(name not in seen, "duplicate service name", name=name)
        seen.add(name)
        names.add(name)
        replicas = row.get("replicas")
        cpu = row.get("cpu_per_replica")
        mem = row.get("mem_per_replica")
        priority = row.get("priority")
        _require(_is_int(replicas) and replicas >= 0,
                 "replicas must be a non negative integer", name=name)
        _require(_is_int(cpu) and cpu > 0,
                 "cpu_per_replica must be a positive integer", name=name)
        _require(_is_int(mem) and mem > 0,
                 "mem_per_replica must be a positive integer", name=name)
        _require(_is_int(priority), "priority must be an integer", name=name)
        upstream = row.get("upstream", [])
        _require(isinstance(upstream, list), "upstream must be a list", name=name)
        _require(name not in upstream, "service cannot depend on itself", name=name)
        fd = row.get("failure_domain")
        _require(isinstance(fd, str) and fd, "failure_domain missing", name=name)
        services.append(
            Service(
                name=name,
                replicas=replicas,
                cpu_per_replica=cpu,
                mem_per_replica=mem,
                upstream=tuple(upstream),
                failure_domain=fd,
                priority=priority,
            )
        )
    # upstream references and DAG acyclicity are checked once names are known
    for service in services:
        for up in service.upstream:
            _require(up in names, "upstream service does not exist",
                     service=service.name, upstream=up)
    _assert_acyclic({s.name: set(s.upstream) for s in services})
    return services


def _assert_acyclic(graph):
    visiting = set()
    visited = set()

    def visit(node):
        if node in visiting:
            raise bad_sample("dependency cycle detected", node=node)
        if node in visited:
            return
        visiting.add(node)
        for nxt in graph[node]:
            visit(nxt)
        visiting.discard(node)
        visited.add(node)

    for node in graph:
        visit(node)


# ------------------------------------------------------------------------ nodes
def parse_nodes(payload):
    _require(isinstance(payload, list) and payload, "nodes must be a non empty list")
    nodes = []
    seen = set()
    for index, row in enumerate(payload):
        _require(isinstance(row, dict), "node entry must be an object", index=index)
        name = row.get("name")
        _require(isinstance(name, str) and name, "node name missing", index=index)
        _require(name not in seen, "duplicate node name", name=name)
        seen.add(name)
        cpu_total = row.get("cpu_total")
        mem_total = row.get("mem_total")
        cpu_free = row.get("cpu_free", cpu_total)
        mem_free = row.get("mem_free", mem_total)
        topo = row.get("topology_domain")
        for field_name, value in (
            ("cpu_total", cpu_total),
            ("mem_total", mem_total),
            ("cpu_free", cpu_free),
            ("mem_free", mem_free),
        ):
            _require(_is_int(value) and value >= 0,
                     f"{field_name} must be a non negative integer", node=name)
        _require(cpu_free <= cpu_total and mem_free <= mem_total,
                 "free capacity cannot exceed total capacity", node=name)
        _require(isinstance(topo, str) and topo,
                 "topology_domain missing", node=name)
        nodes.append(
            Node(
                name=name,
                cpu_total=cpu_total,
                mem_total=mem_total,
                cpu_free=cpu_free,
                mem_free=mem_free,
                topology_domain=topo,
            )
        )
    return nodes


# --------------------------------------------------------------------- v1 policy
def _parse_ratio(raw):
    try:
        dec = Decimal(str(raw))
    except Exception as exc:  # pragma: no cover - defensive
        raise bad_sample("gear ratio is not numeric", ratio=raw) from exc
    _require(dec > 0, "gear ratio must be positive", ratio=raw)
    return str(dec)


def parse_v1(payload):
    _require(isinstance(payload, list), "v1 policies must be a list")
    policies = []
    seen = set()
    for row in payload:
        _require(isinstance(row, dict), "v1 policy must be an object")
        service = row.get("service")
        _require(isinstance(service, str) and service, "v1 service missing")
        _require(service not in seen, "duplicate v1 policy", service=service)
        seen.add(service)
        gears_raw = row.get("gears")
        _require(isinstance(gears_raw, list) and gears_raw,
                 "v1 gears must be a non empty list", service=service)
        gears = []
        ratios = []
        for gear in gears_raw:
            rel = gear.get("relative_seconds")
            _require(_is_int(rel) and rel >= 0,
                     "relative_seconds must be a non negative integer",
                     service=service)
            ratio = _parse_ratio(gear.get("ratio"))
            gears.append(Gear(relative_seconds=rel, ratio=ratio))
            ratios.append(Decimal(ratio))
        for previous, current in zip(ratios, ratios[1:]):
            _require(current > previous,
                     "v1 gear ratios must be strictly increasing", service=service)
        _require(Decimal("1") in ratios,
                 "v1 gear ratios must include the baseline ratio 1",
                 service=service)
        policies.append(V1Policy(service=service, gears=tuple(gears)))
    return policies


# --------------------------------------------------------------------- v2 shards
def read_v2_metadata(meta_db_path):
    """Read shard metadata from the v2 SQLite metadata database."""
    connection = sqlite3.connect(f"file:{meta_db_path}?mode=ro", uri=True)
    try:
        connection.row_factory = sqlite3.Row
        rows = connection.execute(
            "SELECT shard_id, generation, shard_hash FROM shard_meta "
            "ORDER BY shard_id"
        ).fetchall()
        return {row["shard_id"]: dict(row) for row in rows}
    finally:
        connection.close()


def parse_v2_shard(payload, metadata):
    _require(isinstance(payload, dict), "v2 shard must be an object")
    shard_id = payload.get("shard_id")
    _require(isinstance(shard_id, str) and shard_id, "shard_id missing")
    meta = metadata.get(shard_id)
    if meta is None:
        raise bad_shard("shard missing from metadata database", shard_id=shard_id)
    generation = payload.get("generation")
    shard_hash = payload.get("shard_hash")
    declared_clock = payload.get("declared_clock_ms")
    if not _is_int(generation) or generation < 0:
        raise bad_shard("generation must be a non negative integer",
                        shard_id=shard_id)
    if generation != meta["generation"]:
        raise bad_shard("generation does not match metadata",
                        shard_id=shard_id, json_generation=generation,
                        meta_generation=meta["generation"])
    if not isinstance(shard_hash, str) or shard_hash != meta["shard_hash"]:
        raise bad_shard("shard hash does not match metadata", shard_id=shard_id)
    if not _is_int(declared_clock) or declared_clock < 0:
        raise bad_shard("declared_clock_ms must be a non negative integer",
                        shard_id=shard_id)
    services = payload.get("services", [])
    entries = payload.get("policies", [])
    if not isinstance(services, list) or not isinstance(entries, list):
        raise bad_shard("services/policies must be lists", shard_id=shard_id)
    if len(set(services)) != len(services):
        raise bad_shard("duplicate service inside shard", shard_id=shard_id)

    gears = []
    tombstones = list(payload.get("tombstones", []))
    last_generation = None
    last_clock_by_service = {}
    for entry in entries:
        service = entry.get("service")
        if service not in services:
            raise bad_shard("policy service not declared in shard",
                            shard_id=shard_id, service=service)
        entry_generation = entry.get("generation", generation)
        if not _is_int(entry_generation) or entry_generation < 0:
            raise bad_shard("policy generation must be a non negative integer",
                            shard_id=shard_id, service=service)
        if last_generation is not None and entry_generation < last_generation:
            raise bad_shard("policy generation must be monotonic within shard",
                            shard_id=shard_id, service=service)
        last_generation = entry_generation
        for gear in entry.get("gears", []):
            absolute_ms = gear.get("absolute_ms")
            if not _is_int(absolute_ms):
                raise bad_shard("absolute_ms must be an integer",
                                shard_id=shard_id, service=service)
            previous_clock = last_clock_by_service.get(service)
            if previous_clock is not None and absolute_ms < previous_clock:
                raise bad_shard("absolute_ms must be monotonic within shard",
                                shard_id=shard_id, service=service)
            last_clock_by_service[service] = absolute_ms
            ratio = _parse_ratio(gear.get("ratio"))
            gears.append((service, entry_generation,
                          V2Gear(absolute_ms=absolute_ms, ratio=ratio)))
    for tombstone in tombstones:
        if not isinstance(tombstone, dict):
            raise bad_shard("tombstone must be an object", shard_id=shard_id)
        if not _is_int(tombstone.get("generation")):
            raise bad_shard("tombstone generation missing", shard_id=shard_id)
    return V2Shard(
        shard_id=shard_id,
        generation=generation,
        shard_hash=shard_hash,
        declared_clock_ms=declared_clock,
        services=tuple(services),
        gears=tuple(gears),
        tombstones=tuple(tombstones),
    )


def validate_shard_order(shards):
    """Generations and hashes must be monotonic across the ordered shards."""
    previous_generation = None
    previous_hash = None
    for shard in shards:
        if previous_generation is not None:
            if shard.generation < previous_generation:
                raise bad_shard("shard generation rollback across shards",
                                shard_id=shard.shard_id)
            if shard.generation == previous_generation and \
                    shard.shard_hash == previous_hash:
                raise bad_shard("duplicate shard hash at same generation",
                                shard_id=shard.shard_id)
        previous_generation = shard.generation
        previous_hash = shard.shard_hash


# ------------------------------------------------------------------------- load
def parse_load(payload):
    _require(isinstance(payload, list) and payload,
             "load trace must be a non empty list")
    trace = {}
    last_ms_by_service = {}
    for row in payload:
        service = row.get("service")
        at_ms = row.get("at_ms")
        load = row.get("load")
        _require(isinstance(service, str) and service, "load service missing")
        _require(_is_int(at_ms) and at_ms >= 0, "at_ms must be non negative int")
        previous = last_ms_by_service.get(service)
        if previous is not None and at_ms < previous:
            raise bad_sample("load trace must be time ordered", service=service)
        last_ms_by_service[service] = at_ms
        try:
            load_dec = Decimal(str(load))
        except Exception as exc:  # pragma: no cover - defensive
            raise bad_sample("load must be numeric", service=service) from exc
        _require(load_dec >= 0 and load_dec <= 1,
                 "load must be in [0, 1]", service=service)
        trace[(service, at_ms)] = str(load_dec)
    return trace


# ------------------------------------------------------------------------- config
DEFAULT_CONFIG = {
    "round_budget_ms": 1000,
    "action_cost_ms": 120,
    "fd_concurrency": 2,
    "tolerance_ticks": 5,
    "fixed_scale": 1000,
    "clock_step_ms": 100,
    "cost_per_replica": "1.5",
    "round_budget_cost": "1000",
}


def parse_config(payload):
    _require(isinstance(payload, dict), "config must be an object")
    config = dict(DEFAULT_CONFIG)
    config.update(payload)
    for key in (
        "round_budget_ms",
        "action_cost_ms",
        "fd_concurrency",
        "tolerance_ticks",
        "fixed_scale",
        "clock_step_ms",
    ):
        _require(_is_int(config[key]) and config[key] > 0,
                 f"{key} must be a positive integer", value=config[key])
    for key in ("cost_per_replica", "round_budget_cost"):
        try:
            dec = Decimal(str(config[key]))
        except Exception as exc:  # pragma: no cover - defensive
            raise bad_sample(f"{key} must be numeric") from exc
        _require(dec > 0, f"{key} must be positive")
        config[key] = str(dec)
    return config
