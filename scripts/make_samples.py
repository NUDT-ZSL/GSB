"""生成确定性本地样本：v1 单文件 JSON、v2 分片 JSON + SQLite 元数据库、逻辑时钟步进表。

所有输出均为确定性内容（固定 JSON 格式与排序），重复生成逐位一致。
"""

import hashlib
import json
import os
import sqlite3

ROOT = os.path.join(os.path.dirname(__file__), "..", "samples")


def dump(path, obj):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(json.dumps(obj, sort_keys=True, indent=2, ensure_ascii=False) + "\n")


def sha256_file(path):
    with open(path, "rb") as fh:
        return hashlib.sha256(fh.read()).hexdigest()


def main():
    # ---- v1 单文件配置：配置级逻辑时钟 5000ms ----
    v1 = {
        "format": "recovery-v1",
        "logical_clock_ms": 5000,
        "services": [
            {"name": "web", "failure_domain": "fd-1", "fallback_seconds": 30,
             "upstreams": ["api"], "ladder": [10, 25, 50, 100],
             "actions": [
                 {"id": "web-stop", "kind": "stop", "at_tick": 10, "script": "stop web"},
                 {"id": "web-switch", "kind": "switch", "at_tick": 20, "script": "switch web"},
             ]},
            {"name": "api", "failure_domain": "fd-1", "fallback_seconds": 20,
             "upstreams": ["db"], "ladder": [10, 25, 50, 100],
             "actions": [
                 {"id": "api-stop", "kind": "stop", "at_tick": 5, "script": "stop api"},
                 {"id": "api-switch", "kind": "switch", "at_tick": 15, "script": "switch api"},
             ]},
            {"name": "legacy", "failure_domain": "fd-2", "fallback_seconds": 0,
             "upstreams": [], "ladder": [50, 100],
             "actions": [
                 {"id": "legacy-switch", "kind": "switch", "at_tick": 1, "script": "switch legacy"},
             ]},
            {"name": "tie", "failure_domain": "fd-2", "fallback_seconds": 5,
             "upstreams": [], "ladder": [100],
             "actions": [
                 {"id": "tie-switch", "kind": "switch", "at_tick": 3, "script": "switch tie"},
             ]},
        ],
        "service_actions": {
            "web": ["web-stop", "web-switch"],
            "api": ["api-stop", "api-switch"],
            "legacy": ["legacy-switch"],
            "tie": ["tie-switch"],
        },
    }
    dump(os.path.join(ROOT, "v1", "config.json"), v1)

    # ---- v2 分片配置 ----
    shard_001 = {
        "shard_id": "shard_001",
        "services": [
            {"name": "db", "failure_domain": "fd-0", "effective_ms": 9000, "generation": 3,
             "upstreams": [], "tombstoned": False, "ladder": [10, 25, 50, 100],
             "actions": [
                 {"id": "db-stop", "kind": "stop", "at_ms": 9010, "priority": "P0",
                  "script": "stop db"},
                 {"id": "db-switch", "kind": "switch", "at_ms": 9020, "priority": "P0",
                  "script": "switch db"},
             ]},
            {"name": "api", "failure_domain": "fd-1", "effective_ms": 9000, "generation": 3,
             "upstreams": ["db"], "tombstoned": False, "ladder": [10, 25, 50, 100],
             "actions": [
                 {"id": "api-stop-v2", "kind": "stop", "at_ms": 9030, "priority": "P1",
                  "script": "stop api v2"},
                 {"id": "api-switch-v2", "kind": "switch", "at_ms": 9040, "priority": "P1",
                  "script": "switch api v2"},
             ]},
        ],
    }
    shard_002 = {
        "shard_id": "shard_002",
        "services": [
            {"name": "cache", "failure_domain": "fd-1", "effective_ms": 9000, "generation": 3,
             "upstreams": ["db"], "tombstoned": False, "ladder": [25, 50, 100],
             "actions": [
                 {"id": "cache-switch", "kind": "switch", "at_ms": 9050, "priority": "P2",
                  "script": "switch cache"},
             ]},
            {"name": "web", "failure_domain": "fd-1", "effective_ms": 4000, "generation": 2,
             "upstreams": ["api"], "tombstoned": False, "ladder": [10, 25, 50, 100],
             "actions": [
                 {"id": "web-switch-v2", "kind": "switch", "at_ms": 4010, "priority": "P1",
                  "script": "switch web v2"},
             ]},
            {"name": "tie", "failure_domain": "fd-2", "effective_ms": 5000, "generation": 3,
             "upstreams": [], "tombstoned": False, "ladder": [100],
             "actions": [
                 {"id": "tie-switch-v2", "kind": "switch", "at_ms": 5010, "priority": "P1",
                  "script": "switch tie v2"},
             ]},
            {"name": "oldsvc", "failure_domain": "fd-3", "effective_ms": 9000, "generation": 2,
             "upstreams": [], "tombstoned": True, "ladder": [100],
             "actions": [
                 {"id": "oldsvc-switch", "kind": "switch", "at_ms": 9060, "priority": "P3",
                  "script": "switch oldsvc"},
             ]},
        ],
    }
    dump(os.path.join(ROOT, "v2", "shards", "shard_001.json"), shard_001)
    dump(os.path.join(ROOT, "v2", "shards", "shard_002.json"), shard_002)

    # ---- v2 SQLite 元数据库 ----
    meta_path = os.path.join(ROOT, "v2", "meta.sqlite")
    if os.path.exists(meta_path):
        os.remove(meta_path)
    conn = sqlite3.connect(meta_path)
    conn.execute("CREATE TABLE shards (shard_id TEXT PRIMARY KEY, sha256 TEXT NOT NULL)")
    conn.execute("CREATE TABLE generation_floor (scope TEXT PRIMARY KEY, generation_floor INTEGER NOT NULL)")
    for shard_id in ("shard_001", "shard_002"):
        digest = sha256_file(os.path.join(ROOT, "v2", "shards", shard_id + ".json"))
        conn.execute("INSERT INTO shards VALUES (?, ?)", (shard_id, digest))
    conn.execute("INSERT INTO generation_floor VALUES ('global', 2)")
    conn.commit()
    conn.close()

    # ---- 注入逻辑时钟步进表 ----
    dump(os.path.join(ROOT, "clock_steps.json"),
         {"tolerance_ms": 50, "steps_ms": [10000 + i * 100 for i in range(400)]})

    print("samples generated under", os.path.abspath(ROOT))


if __name__ == "__main__":
    main()
