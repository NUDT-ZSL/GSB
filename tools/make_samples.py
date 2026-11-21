"""Generate the fixed local sample set deterministically.

Produces, under samples/:
  v1/snapshot_v1.json      single-file v1 snapshot
  v2/shards/shard_*.json   sharded v2 snapshots
  v2/meta.sqlite           v2 SQLite metadata (versions, tombstones,
                           fragment checksums, generation)
  fragments/<sha256>       content-addressed fragment files (one corrupted)
  templates/*.json         legal and invalid query templates
  manifest.json            fragment digests, shard digests, logical clocks
  config.json              injected paths (no wall clock anywhere)

The sample set intentionally contains: conflict documents decided by
version / clock / source priority, a tombstoned document, and a fragment
whose on-disk bytes fail checksum verification."""

import hashlib
import json
import os
import shutil
import sqlite3
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SAMPLES = os.path.join(ROOT, "samples")


def sha(data):
    return hashlib.sha256(data).hexdigest()


def write_json(path, obj):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(json.dumps(obj, sort_keys=True, indent=2,
                            ensure_ascii=False) + "\n")


def write_bytes(path, data):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb") as fh:
        fh.write(data)


def main():
    if os.path.isdir(SAMPLES):
        shutil.rmtree(SAMPLES)
    os.makedirs(SAMPLES)
    intended = {
        "frag1": b"status=active;score=20;",
        "frag2": b"status=archived;score=42;",
        "frag3": b"level=gold;region=eu;",
        "frag4": b"level=silver;region=us;",
        "frag5": b"status=retired;score=7;",
        "frag6": b"tag=clock-field;",
        "frag7": b"tag=clock-v1-field;",
        "frag8": b"tag=priority-tie-field;",
    }
    corrupted = {"frag4": b"level=SILVER!!corrupted;"}
    checksums = {name: sha(data) for name, data in intended.items()}
    for name, data in intended.items():
        write_bytes(
            os.path.join(SAMPLES, "fragments", checksums[name]),
            corrupted.get(name, data),
        )

    def span(frag, offset, length, field):
        return {
            "field": field,
            "fragment": checksums[frag],
            "offset": offset,
            "length": length,
        }

    v1_docs = [
        {"doc_id": "doc-001", "version": 1, "clock_ms": 1000,
         "fields": {"score": 15, "status": "active"},
         "spans": [span("frag1", 0, 13, "status"),
                   span("frag1", 14, 8, "score")]},
        {"doc_id": "doc-002", "version": 2, "clock_ms": 1100,
         "fields": {"score": 42, "status": "archived"},
         "spans": [span("frag2", 0, 15, "status"),
                   span("frag2", 16, 8, "score")]},
        {"doc_id": "doc-004", "version": 1, "clock_ms": 1200,
         "fields": {"level": "silver", "region": "us"},
         "spans": [span("frag4", 0, 12, "level"),
                   span("frag4", 13, 9, "region")]},
        {"doc_id": "doc-005", "version": 1, "clock_ms": 1300,
         "fields": {"score": 7, "status": "retired"},
         "spans": [span("frag5", 0, 14, "status"),
                   span("frag5", 15, 7, "score")]},
        {"doc_id": "doc-006", "version": 2, "clock_ms": 1200,
         "fields": {"tag": "clock-v1"},
         "spans": [span("frag6", 0, 16, "tag")]},
        {"doc_id": "doc-007", "version": 3, "clock_ms": 2500,
         "fields": {"tag": "clock-v1"},
         "spans": [span("frag7", 0, 19, "tag")]},
        {"doc_id": "doc-008", "version": 4, "clock_ms": 2000,
         "fields": {"tag": "tie-v1"},
         "spans": [span("frag8", 0, 21, "tag")]},
    ]
    v1_inverted = [
        {"doc_id": d["doc_id"], "field": f, "value": v}
        for d in v1_docs
        for f, v in sorted(d["fields"].items())
    ]
    write_json(
        os.path.join(SAMPLES, "v1", "snapshot_v1.json"),
        {
            "snapshot_id": "v1-0001",
            "format": "v1",
            "clock_ms": 1500,
            "documents": v1_docs,
            "inverted": v1_inverted,
        },
    )
    shard0_docs = [
        {"doc_id": "doc-001", "version": 2, "clock_ms": 2000,
         "fields": {"score": 20, "status": "active"},
         "spans": [span("frag1", 0, 13, "status"),
                   span("frag1", 14, 8, "score")]},
        {"doc_id": "doc-003", "version": 1, "clock_ms": 1900,
         "fields": {"level": "gold", "region": "eu"},
         "spans": [span("frag3", 0, 10, "level"),
                   span("frag3", 11, 9, "region")]},
        {"doc_id": "doc-005", "version": 1, "clock_ms": 1400,
         "fields": {"score": 7, "status": "retired"},
         "spans": [span("frag5", 0, 14, "status"),
                   span("frag5", 15, 7, "score")]},
        {"doc_id": "doc-006", "version": 2, "clock_ms": 1800,
         "fields": {"tag": "clock-v2"},
         "spans": [span("frag6", 0, 16, "tag")]},
    ]
    shard1_docs = [
        {"doc_id": "doc-007", "version": 3, "clock_ms": 1800,
         "fields": {"tag": "clock-v2"},
         "spans": [span("frag7", 0, 19, "tag")]},
        {"doc_id": "doc-008", "version": 4, "clock_ms": 2000,
         "fields": {"tag": "tie-v2"},
         "spans": [span("frag8", 0, 21, "tag")]},
    ]
    shard_paths = []
    for idx, docs in enumerate([shard0_docs, shard1_docs]):
        rel = os.path.join("v2", "shards", "shard_%03d.json" % idx)
        write_json(
            os.path.join(SAMPLES, rel),
            {
                "shard_id": "shard_%03d" % idx,
                "format": "v2",
                "snapshot_id": "v2-0001",
                "clock_ms": 2000,
                "documents": docs,
            },
        )
        shard_paths.append(rel)
    meta_db = os.path.join(SAMPLES, "v2", "meta.sqlite")
    if os.path.exists(meta_db):
        os.remove(meta_db)
    con = sqlite3.connect(meta_db)
    con.executescript(
        """
        CREATE TABLE documents (
            doc_id TEXT PRIMARY KEY,
            version INTEGER NOT NULL,
            clock_ms INTEGER NOT NULL
        );
        CREATE TABLE tombstones (
            doc_id TEXT PRIMARY KEY,
            version INTEGER NOT NULL,
            clock_ms INTEGER NOT NULL
        );
        CREATE TABLE fragments (
            checksum TEXT PRIMARY KEY,
            size INTEGER NOT NULL
        );
        CREATE TABLE meta (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL
        );
        """
    )
    for doc in shard0_docs + shard1_docs:
        con.execute(
            "INSERT INTO documents(doc_id, version, clock_ms) VALUES(?, ?, ?)",
            (doc["doc_id"], doc["version"], doc["clock_ms"]),
        )
    con.execute(
        "INSERT INTO tombstones(doc_id, version, clock_ms) VALUES(?, ?, ?)",
        ("doc-005", 1, 2100),
    )
    for name in ["frag1", "frag3", "frag5", "frag6", "frag7", "frag8"]:
        con.execute(
            "INSERT INTO fragments(checksum, size) VALUES(?, ?)",
            (checksums[name], len(intended[name])),
        )
    for key, value in [
        ("clock_ms", "2000"),
        ("generation", "1"),
        ("snapshot_id", "v2-0001"),
    ]:
        con.execute("INSERT INTO meta(key, value) VALUES(?, ?)", (key, value))
    con.commit()
    con.close()
    write_json(
        os.path.join(SAMPLES, "templates", "legal.json"),
        [
            {"template_id": "t1-eq-active-fetch", "op": "eq",
             "field": "status", "eq": "active", "fetch_fragments": True},
            {"template_id": "t2-range-score", "op": "range",
             "field": "score", "range": [10, 50]},
            {"template_id": "t3-and-level-region", "op": "and",
             "clauses": [
                 {"field": "level", "eq": "gold"},
                 {"field": "region", "eq": "eu"},
             ]},
            {"template_id": "t4-eq-silver-fetch-corrupt", "op": "eq",
             "field": "level", "eq": "silver", "fetch_fragments": True},
            {"template_id": "t5-eq-tombstoned", "op": "eq",
             "field": "status", "eq": "retired"},
            {"template_id": "t6-eq-no-hit", "op": "eq",
             "field": "status", "eq": "nonexistent"},
        ],
    )
    write_json(
        os.path.join(SAMPLES, "templates", "invalid_syntax.json"),
        [{"template_id": "bad-syntax", "op": "between",
          "field": "score", "range": [1, 2]}],
    )
    write_json(
        os.path.join(SAMPLES, "templates", "type_mismatch.json"),
        [{"template_id": "bad-type", "op": "range",
          "field": "status", "range": [1, 2]}],
    )
    manifest = {
        "sample_id": "fixed-001",
        "shards": [
            {
                "path": rel,
                "sha256": sha(
                    open(os.path.join(SAMPLES, rel), "rb").read()
                ),
            }
            for rel in shard_paths
        ],
        "fragments": [
            {
                "checksum": checksums[name],
                "size": len(intended[name]),
                "sha256": checksums[name],
            }
            for name in sorted(intended)
        ],
        "clocks": {
            "load": 3000,
            "merge": 3100,
            "query": 3200,
            "regressed": 1000,
            "crash_merge": 3210,
            "crash_query": 3220,
            "resume": 3230,
            "post_resume": 3240,
        },
    }
    write_json(os.path.join(SAMPLES, "manifest.json"), manifest)
    write_json(
        os.path.join(SAMPLES, "config.json"),
        {
            "base_dir": "..",
            "v1_snapshot": "samples/v1/snapshot_v1.json",
            "v2_shard_dir": "samples/v2/shards",
            "v2_meta_db": "samples/v2/meta.sqlite",
            "fragment_dir": "samples/fragments",
            "manifest": "samples/manifest.json",
            "templates_dir": "samples/templates",
            "state_db": "workspace/state.sqlite",
            "lock_file": "workspace/engine.lock",
            "clock_tolerance_ms": 0,
        },
    )
    print("samples written to %s" % os.path.relpath(SAMPLES, ROOT))
    return 0


if __name__ == "__main__":
    sys.exit(main())
