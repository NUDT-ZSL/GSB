"""Deterministic fixed-seed local sample generator (no network, no real hosts)."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sqlite3

import numpy as np

from . import atomic
from .spec import BaselineSpec

SEED = 20261125
CLOCK_MS = 1_700_000_000_000


def _digest(payload: dict) -> str:
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


def build_spec() -> dict:
    return {
        "merge_version": "1.0.0",
        "whitelist_version": "1.0.0",
        "observation_clock_ms": CLOCK_MS,
        "hosts": ["host-a", "host-b", "host-c"],
        "host_service": {
            "host-a": "svc-auth",
            "host-b": "svc-auth",
            "host-c": "svc-edge",
        },
        "configs": [
            {"key": "net.keepalive", "flat_alias": "net__keepalive", "type": "int", "upper": 60},
            {"key": "tls.strict", "flat_alias": "tls__strict", "type": "bool", "upper": True},
            {"key": "auth.mode", "flat_alias": "auth__mode", "type": "enum",
             "enum": ["STRICT", "COMPAT", "LEGACY"], "upper": "STRICT"},
            {"key": "rate.limit[0].window", "flat_alias": "rate__limit_0_window",
             "type": "interval", "upper": 1000},
            {"key": "firewall.mask", "flat_alias": "firewall__mask", "type": "mask",
             "enum": ["SSH", "HTTP", "DBG", "ADMIN"], "upper": ["HTTP", "SSH"]},
            # cycle trio: queue -> worker -> backlog -> queue
            {"key": "queue.depth", "flat_alias": "queue__depth", "type": "int", "upper": 10},
            {"key": "worker.count", "flat_alias": "worker__count", "type": "int", "upper": 8},
            {"key": "backlog.size", "flat_alias": "backlog__size", "type": "int", "upper": 32},
            {"key": "edge.buffer", "flat_alias": "edge__buffer", "type": "int", "upper": 128},
        ],
        "dependencies": [
            {"dependent": "edge.buffer", "depends_on": "net.keepalive"},
            {"dependent": "worker.count", "depends_on": "queue.depth"},
            {"dependent": "backlog.size", "depends_on": "worker.count"},
            {"dependent": "queue.depth", "depends_on": "backlog.size"},
        ],
        "freeze": {"host-a": ["auth.mode"]},
    }


def generate(output_dir: str, seed: int = SEED) -> dict:
    rng = np.random.default_rng(seed)
    spec_payload = build_spec()
    spec = BaselineSpec(spec_payload)
    os.makedirs(output_dir, exist_ok=True)
    atomic.atomic_write_json(os.path.join(output_dir, "spec.json"), spec_payload)

    # ---------------- v1 flat snapshot ----------------
    v1_records = [
        {"host": "host-a", "flat_key": "net__keepalive", "relative_seconds": 30,
         "observed": "90", "severity": "HIGH", "drift": True, "algebra": 1},
        {"host": "host-a", "flat_key": "tls__strict", "relative_seconds": 10,
         "observed": "false", "severity": "MEDIUM", "drift": True, "algebra": 1},
        {"host": "host-a", "flat_key": "auth__mode", "relative_seconds": 5,
         "observed": "LEGACY", "severity": "HIGH", "drift": True, "algebra": 1},
        {"host": "host-b", "flat_key": "rate__limit_0_window", "relative_seconds": 12,
         "observed": "[100,900]", "severity": "LOW", "drift": False, "algebra": 1},
        {"host": "host-b", "flat_key": "firewall__mask", "relative_seconds": 20,
         "observed": "SSH|HTTP", "severity": "LOW", "drift": False, "algebra": 1},
        {"host": "host-c", "flat_key": "queue__depth", "relative_seconds": 40,
         "observed": "25", "severity": "HIGH", "drift": True, "algebra": 1},
        {"host": "host-c", "flat_key": "edge__buffer", "relative_seconds": 8,
         "observed": "200", "severity": "MEDIUM", "drift": True, "algebra": 1},
        # rejected: negative timestamp (unique key), unknown host, duplicate (appended below)
        {"host": "host-a", "flat_key": "net__keepalive", "relative_seconds": -1,
         "observed": "91", "severity": "HIGH", "drift": True, "algebra": 1},
        {"host": "host-b", "flat_key": "net__keepalive", "relative_seconds": -5,
         "observed": "120", "severity": "HIGH", "drift": True, "algebra": 1},
        {"host": "host-zzz", "flat_key": "tls__strict", "relative_seconds": 1,
         "observed": "true", "severity": "LOW", "drift": False, "algebra": 1},
    ]
    v1_records.append(dict(v1_records[0]))  # duplicate host/flat_key
    v1 = {
        "snapshot_id": "v1-flat-0001",
        "generation": 1,
        "observation_clock_ms": CLOCK_MS,
        "records": v1_records,
    }
    v1_dir = os.path.join(output_dir, "v1")
    os.makedirs(v1_dir, exist_ok=True)
    atomic.atomic_write_json(os.path.join(v1_dir, "snapshot-v1.json"), v1)

    # ---------------- v2 sharded snapshot + SQLite index ----------------
    v2_dir = os.path.join(output_dir, "v2", "shards")
    os.makedirs(v2_dir, exist_ok=True)
    shard_records: dict[str, dict] = {"shard-1.json": {}, "shard-2.json": {}}

    def add_v2(shard: str, host: str, key: str, algebra: int, observed_ms: int,
               normalized, tombstone: bool, drift: bool, severity=None):
        record = {
            "host": host, "key": key, "algebra": algebra,
            "observed_ms": observed_ms, "normalized": normalized,
            "tombstone": tombstone, "drift": drift, "severity": severity,
        }
        record["digest"] = _digest({
            "host": host, "key": key, "algebra": algebra, "observed_ms": observed_ms,
            "tombstone": tombstone, "normalized": None if tombstone else normalized,
        })
        record_key = f"{host}|{key}|{algebra}"
        shard_records[shard][record_key] = record
        return record

    # host-a net.keepalive: later v2 value disagrees with v1 -> E_CONFLICT
    add_v2("shard-1.json", "host-a", "net.keepalive", 2, CLOCK_MS + 45_000,
           {"type": "int", "value": 75}, False, True, "HIGH")
    # host-a tls.strict: v2 agrees with v1 normalized value
    add_v2("shard-1.json", "host-a", "tls.strict", 2, CLOCK_MS + 20_000,
           {"type": "bool", "value": False}, False, True, "MEDIUM")
    # host-a auth.mode frozen; v2 tombstone revokes an earlier v2 drift
    add_v2("shard-1.json", "host-a", "auth.mode", 2, CLOCK_MS + 8_000,
           {"type": "enum", "value": "LEGACY"}, False, True, "HIGH")
    add_v2("shard-1.json", "host-a", "auth.mode", 3, CLOCK_MS + 60_000,
           None, True, False, None)
    # host-b interval within bound, then a reversed-interval invalid record
    add_v2("shard-2.json", "host-b", "rate.limit[0].window", 2, CLOCK_MS + 15_000,
           {"type": "interval", "low": 50, "high": 800}, False, False, "LOW")
    add_v2("shard-2.json", "host-b", "rate.limit[0].window", 3, CLOCK_MS + 16_000,
           {"type": "interval", "low": 900, "high": 100}, False, False, "LOW")
    # host-b mask expands -> containment relation, still inside whitelist bound
    add_v2("shard-2.json", "host-b", "firewall.mask", 2, CLOCK_MS + 25_000,
           {"type": "mask", "labels": ["ADMIN", "HTTP", "SSH"]}, False, True, "MEDIUM")
    # host-c edge.buffer drifts near the window edge -> quantization ambiguity
    add_v2("shard-2.json", "host-c", "edge.buffer", 2, CLOCK_MS + 11_500,
           {"type": "int", "value": 210}, False, True, "MEDIUM")

    for shard_id, records in shard_records.items():
        atomic.atomic_write_json(os.path.join(v2_dir, shard_id),
                                 {"shard_id": shard_id, "records": records})

    index_rows = []
    for shard_id, records in shard_records.items():
        for record_key, record in sorted(
                records.items(),
                key=lambda item: (item[1]["host"], item[1]["key"], item[1]["algebra"])):
            index_rows.append((
                record["host"], record["key"], record["algebra"], record["observed_ms"],
                int(record["tombstone"]), record["digest"], shard_id,
            ))
    # Invalid monotonicity: same host/key with a non-increasing algebra.
    index_rows.append(("host-c", "edge.buffer", 1, CLOCK_MS + 9_000, 0,
                       "deadbeef", "shard-2.json"))
    sqlite_path = os.path.join(output_dir, "v2", "metadata.db")
    if os.path.exists(sqlite_path):
        os.remove(sqlite_path)
    connection = sqlite3.connect(sqlite_path)
    connection.execute(
        "CREATE TABLE evidence_index (id INTEGER PRIMARY KEY AUTOINCREMENT, host TEXT, "
        "key TEXT, algebra INTEGER, observed_ms INTEGER, tombstone INTEGER, "
        "digest TEXT, shard_id TEXT)"
    )
    connection.executemany(
        "INSERT INTO evidence_index(host,key,algebra,observed_ms,tombstone,digest,shard_id) "
        "VALUES (?,?,?,?,?,?,?)", index_rows
    )
    connection.commit()
    connection.close()

    manifest = {
        "base_dir": output_dir,
        "v1_globs": ["v1/*.json"],
        "v2_targets": [{"shard_dir": "v2/shards", "sqlite": "v2/metadata.db"}],
    }
    atomic.atomic_write_json(os.path.join(output_dir, "manifest.json"), manifest)
    # touch rng so the seed dependency is explicit and stable
    _ = int(rng.integers(0, 1_000_000))
    return manifest


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="drift-samplegen")
    parser.add_argument("--output-dir", required=True)
    parser.add_argument("--seed", type=int, default=SEED)
    args = parser.parse_args(argv)
    manifest = generate(args.output_dir, args.seed)
    print(json.dumps({"output_dir": args.output_dir, "manifest": manifest}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
