"""Deterministic sample generator.

Generates, from a fixed seed and without any network access:
  - nodes.json          node manifest
  - events.jsonl        election event stream
  - subscriptions.json  subscription declarations
  - topo_v1.json        v1 flat topology snapshot (relative seconds)
  - topo_v2/shard_*.json + meta.sqlite   v2 sharded snapshot (absolute ms)

Re-running with the same seed produces byte-identical output.
"""

from __future__ import annotations

import hashlib
import json
import random
import sqlite3
from pathlib import Path
from typing import Any, Dict, List

DEFAULT_SEED = 20261127

ROLES = ("COORDINATOR", "CANDIDATE", "FOLLOWER", "OBSERVER")
HEALTHS = ("HEALTHY", "DEGRADED", "SUSPECT")
DOMAINS = ("domain-alpha", "domain-beta")
V2_EPOCH = 7


def canonical_json(obj: Any) -> str:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def shard_digest(entries: List[Dict[str, Any]]) -> str:
    return hashlib.sha256(canonical_json(entries).encode("utf-8")).hexdigest()


def _nodes(rng: random.Random) -> List[Dict[str, Any]]:
    nodes = []
    for i in range(8):
        nodes.append(
            {
                "name": f"node-{chr(ord('a') + i)}",
                "role": ROLES[i % len(ROLES)],
                "candidate_addr": f"127.0.0.{i + 1}:{7000 + i}",
                "lease_epoch": 1 + i // 2,
                "priority": rng.randint(1, 100),
                "health": HEALTHS[i % len(HEALTHS)],
            }
        )
    return nodes


def _events(rng: random.Random, nodes: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    events: List[Dict[str, Any]] = []
    seq = 0
    clock = 1_000
    names = [n["name"] for n in nodes]
    # two candidate terms per domain worth of votes, deterministic order
    for term in (5, 6):
        for name in names:
            seq += 1
            clock += rng.randint(20, 120)
            events.append(
                {
                    "seq": seq,
                    "node": name,
                    "clock_ms": clock,
                    "candidate_term": term,
                    "vote": {"for": name, "weight": rng.randint(1, 5)},
                    "revoked": False,
                }
            )
    # a few revocations in term 6 to exercise tombstone paths
    for name in (names[1], names[4]):
        seq += 1
        clock += rng.randint(20, 120)
        events.append(
            {
                "seq": seq,
                "node": name,
                "clock_ms": clock,
                "candidate_term": 6,
                "vote": {"for": name, "weight": 0},
                "revoked": True,
            }
        )
    return events


def _subscriptions(nodes: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    subs = []
    for i, svc in enumerate(("svc-metrics", "svc-audit", "svc-gateway")):
        subs.append(
            {
                "service": svc,
                "subscriber": nodes[(i * 3 + 1) % len(nodes)]["name"],
                "callback": f"http://127.0.0.1:{9000 + i}/hook",
            }
        )
    return subs


def _topo_v1(nodes: List[Dict[str, Any]]) -> Dict[str, Any]:
    # v1 covers the first five nodes with relative publish seconds
    entries = []
    for i, node in enumerate(nodes[:5]):
        entries.append(
            {
                "node": node["name"],
                "view": "flat",
                "rel_publish_s": 5 + i * 3,
                "scope": DOMAINS[i % len(DOMAINS)],
                "depends_on": [nodes[(i + 1) % len(nodes)]["name"]],
            }
        )
    return {"format": "v1", "entries": entries}


def _topo_v2_shards(nodes: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    # v2 covers nodes 3..7 (overlap with v1 on nodes 3,4) plus a tombstone
    covered = nodes[3:]
    shards: List[Dict[str, Any]] = []
    for shard_id in (0, 1):
        half = covered[shard_id * 3 : shard_id * 3 + 3] if shard_id == 0 else covered[3:]
        entries = []
        for j, node in enumerate(half):
            idx = 3 + shard_id * 3 + j
            entries.append(
                {
                    "node": node["name"],
                    "publish_ms": 60_000 + idx * 1_000,
                    "scope": DOMAINS[idx % len(DOMAINS)],
                    "tombstone": node["name"] == "node-g",
                    "depends_on": [nodes[(idx + 2) % len(nodes)]["name"]],
                }
            )
        shards.append(
            {
                "format": "v2",
                "shard": shard_id,
                "epoch": V2_EPOCH,
                "entries": entries,
                "sha256": shard_digest(entries),
            }
        )
    return shards


def generate_all(out_dir: str | Path, seed: int = DEFAULT_SEED) -> Dict[str, Any]:
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    rng = random.Random(seed)

    nodes = _nodes(rng)
    events = _events(rng, nodes)
    subs = _subscriptions(nodes)
    topo_v1 = _topo_v1(nodes)
    shards = _topo_v2_shards(nodes)

    (out / "nodes.json").write_text(
        json.dumps({"nodes": nodes}, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )
    with (out / "events.jsonl").open("w", encoding="utf-8") as fh:
        for ev in events:
            fh.write(canonical_json(ev) + "\n")
    (out / "subscriptions.json").write_text(
        json.dumps({"subscriptions": subs}, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    (out / "topo_v1.json").write_text(
        json.dumps(topo_v1, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )

    v2_dir = out / "topo_v2"
    v2_dir.mkdir(exist_ok=True)
    for shard in shards:
        (v2_dir / f"shard_{shard['shard']}.json").write_text(
            json.dumps(shard, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
        )

    meta_path = v2_dir / "meta.sqlite"
    if meta_path.exists():
        meta_path.unlink()
    conn = sqlite3.connect(str(meta_path))
    with conn:
        conn.execute("CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
        conn.execute(
            "CREATE TABLE shards (shard_id INTEGER PRIMARY KEY, epoch INTEGER NOT NULL, sha256 TEXT NOT NULL)"
        )
        conn.execute("INSERT INTO meta VALUES ('topology_epoch', ?)", (str(V2_EPOCH),))
        for shard in shards:
            conn.execute(
                "INSERT INTO shards VALUES (?, ?, ?)",
                (shard["shard"], shard["epoch"], shard["sha256"]),
            )
    conn.close()

    return {
        "seed": seed,
        "nodes": len(nodes),
        "events": len(events),
        "subscriptions": len(subs),
        "v1_entries": len(topo_v1["entries"]),
        "v2_shards": len(shards),
        "v2_epoch": V2_EPOCH,
        "out_dir": str(out),
    }


if __name__ == "__main__":  # pragma: no cover
    import sys

    target = sys.argv[1] if len(sys.argv) > 1 else "samples"
    print(json.dumps(generate_all(target), indent=2))
