"""Generate the deterministic local samples:
  v1_snapshot.json            v1 cache snapshot (remaining-seconds TTLs)
  v2/                         v2 snapshot package (shards + meta.sqlite)
  v2_incr/                    v2 package -> INCREMENTAL when applied after v2
  v2_full/                    v2 package -> FULL_REPLACE when applied after v2_incr
  v2_rollback/                v2 package with lower version -> rejected
  queries.json                warmup query templates
  clock_steps.json            logical clock step table
  responses.json              preset answers for the loopback DNS test server
All timestamps derive from T0 = 1_000_000_000 us (t = 1000.000000 s).
"""
import hashlib
import json
import os
import sqlite3
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
T0 = 1_000_000_000           # us
NOW_MS = T0 // 1000          # 1_000_000 ms


def write_json(path, obj):
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(obj, fh, indent=2, sort_keys=True)
        fh.write("\n")


def sha256_file(path):
    with open(path, "rb") as fh:
        return hashlib.sha256(fh.read()).hexdigest()


def make_v2_package(directory, version, min_generation, logical_clock_us,
                    shards):
    """shards: {filename: (generation, entries)}"""
    os.makedirs(directory, exist_ok=True)
    shard_meta = []
    for fname, (generation, entries) in sorted(shards.items()):
        write_json(os.path.join(directory, fname), {"entries": entries})
        shard_meta.append((fname, generation))
    meta_path = os.path.join(directory, "meta.sqlite")
    if os.path.exists(meta_path):
        os.remove(meta_path)
    conn = sqlite3.connect(meta_path)
    conn.execute("CREATE TABLE shard_meta("
                 "shard TEXT PRIMARY KEY, generation INTEGER NOT NULL)")
    conn.executemany("INSERT INTO shard_meta VALUES(?,?)", shard_meta)
    conn.commit()
    conn.close()
    manifest = {
        "format": "v2",
        "version": version,
        "min_generation": min_generation,
        "logical_clock_us": logical_clock_us,
        "shards": [{"file": fname, "sha256": sha256_file(
            os.path.join(directory, fname))}
            for fname, _ in shard_meta],
    }
    write_json(os.path.join(directory, "manifest.json"), manifest)


def main():
    # ---- v1 snapshot (declared clock T0, generation 1) ----
    write_json(os.path.join(HERE, "v1_snapshot.json"), {
        "format": "v1",
        "logical_clock_us": T0,
        "generation": 1,
        "entries": [
            {"name": "Alpha.TEST.", "type": "A", "ttl_remaining_s": 60,
             "records": [{"rdata": "192.0.2.10"}], "authoritative": False},
            {"name": "beta.test", "type": "A", "ttl_remaining_s": 30,
             "records": [{"rdata": "192.0.2.20"}], "authoritative": False},
            {"name": "gamma.test", "type": "A", "ttl_remaining_s": 0,
             "records": [{"rdata": "192.0.2.30"}], "authoritative": True},
            {"name": "www.test", "type": "CNAME", "ttl_remaining_s": 120,
             "records": [{"target": "cdn.test"}], "authoritative": False},
            {"name": "zeta.test", "type": "A", "ttl_remaining_s": 45,
             "records": [{"rdata": "198.51.100.5"}], "authoritative": True},
            {"name": "eta.test", "type": "A", "ttl_remaining_s": 90,
             "records": [{"rdata": "192.0.2.90"}], "authoritative": False},
        ],
    })
    # ---- v2 base package (version 2, declared clock T0 + 500ms) ----
    shard0 = [
        {"name": "alpha.test", "type": "A", "level": "recursive",
         "expiry_ms": NOW_MS + 60_500, "negative": False,
         "records": [{"rdata": "192.0.2.10"}]},
        {"name": "beta.test", "type": "A", "level": "recursive",
         "expiry_ms": NOW_MS + 30_400, "negative": False,
         "records": [{"rdata": "192.0.2.20"}]},
        {"name": "delta.test", "type": "A", "level": "recursive",
         "expiry_ms": NOW_MS + 30_000, "negative": True,
         "negative_kind": "NXDOMAIN", "records": []},
    ]
    shard1 = [
        {"name": "epsilon.test", "type": "A", "level": "recursive",
         "expiry_ms": NOW_MS + 30_000, "negative": True,
         "negative_kind": "NODATA", "records": []},
        {"name": "zeta.test", "type": "A", "level": "recursive",
         "expiry_ms": NOW_MS + 45_000, "negative": False,
         "records": [{"rdata": "198.51.100.5"}]},
        {"name": "eta.test", "type": "A", "level": "local",
         "expiry_ms": NOW_MS + 95_000, "negative": False,
         "records": [{"rdata": "192.0.2.90"}]},
    ]
    make_v2_package(os.path.join(HERE, "v2"), 2, 1, T0 + 500_000,
                    {"shard0.json": (2, shard0), "shard1.json": (2, shard1)})
    # ---- v2_incr: version 3, same shards + one new shard -> INCREMENTAL ----
    shard2 = [
        {"name": "theta.test", "type": "A", "level": "recursive",
         "expiry_ms": NOW_MS + 80_000, "negative": False,
         "records": [{"rdata": "203.0.113.20"}]},
    ]
    make_v2_package(os.path.join(HERE, "v2_incr"), 3, 2, T0 + 900_000,
                    {"shard0.json": (2, shard0), "shard1.json": (2, shard1),
                     "shard2.json": (3, shard2)})
    # ---- v2_full: version 4, min_generation 5 -> FULL_REPLACE ----
    shard9 = [
        {"name": "alpha.test", "type": "A", "level": "recursive",
         "expiry_ms": NOW_MS + 70_000, "negative": False,
         "records": [{"rdata": "192.0.2.11"}]},
    ]
    make_v2_package(os.path.join(HERE, "v2_full"), 4, 5, T0 + 1_500_000,
                    {"shard9.json": (5, shard9)})
    # ---- v2_rollback: version 1 -> rejected ----
    make_v2_package(os.path.join(HERE, "v2_rollback"), 1, 0, T0,
                    {"shard0.json": (1, shard0[:1])})
    # ---- query templates + clock steps ----
    write_json(os.path.join(HERE, "queries.json"), {"queries": [
        {"name": "gamma.test", "type": "A"},
        {"name": "alpha.test", "type": "A"},
        {"name": "alias.test", "type": "A"},
        {"name": "nx.test", "type": "A"},
        {"name": "nodata.test", "type": "A"},
        {"name": "delta.test", "type": "A"},
        {"name": "www.test", "type": "A"},
    ]})
    write_json(os.path.join(HERE, "clock_steps.json"), {"steps": [
        {"name": f"t{i}", "now_us": T0 + i * 1_000_000} for i in range(7)
    ]})
    # ---- preset answers for the loopback DNS test server ----
    write_json(os.path.join(HERE, "responses.json"), {
        "port": 15353,
        "default_negative_ttl_s": 30,
        "answers": {
            "gamma.test|A": {"records": [
                {"type": "A", "address": "10.0.0.1", "ttl_s": 20}]},
            "cdn.test|A": {"records": [
                {"type": "A", "address": "203.0.113.7", "ttl_s": 45}]},
            "www.test|A": {"records": [
                {"type": "CNAME", "name": "www.test", "target": "cdn.test",
                 "ttl_s": 120},
                {"type": "A", "name": "cdn.test", "address": "203.0.113.7",
                 "ttl_s": 45}]},
            "alias.test|A": {"records": [
                {"type": "CNAME", "name": "alias.test", "target": "www.test",
                 "ttl_s": 300}]},
            "nx.test|A": {"rcode": "NXDOMAIN", "negative_ttl_s": 25},
            "nodata.test|A": {"records": [], "negative_ttl_s": 35},
            "broken.test|A": {"malformed": True},
            "slow.test|A": {"delay_ms": 300, "records": [
                {"type": "A", "address": "203.0.113.9", "ttl_s": 10}]},
            "theta.test|A": {"records": [
                {"type": "A", "address": "203.0.113.20", "ttl_s": 80}]},
        },
    })
    print("samples generated under", HERE)


if __name__ == "__main__":
    sys.exit(main())
