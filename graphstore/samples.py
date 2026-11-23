from __future__ import annotations

import shutil
import sqlite3
from pathlib import Path
from typing import Any

from .snapshot import (
    PACKAGE_SCHEMA,
    V1_SCHEMA,
    V2_MANIFEST_SCHEMA,
    V2_SHARD_SCHEMA,
    v2_node_digest,
)
from .util import atomic_write_json, canonical_json, digest_bytes


DEFAULT_LOGICAL_CLOCK_MS = 1_700_000_000_000
SECOND = 1_000


def _v1_edge(target: str, label: str, weight: float) -> dict[str, Any]:
    return {"target": target, "label": label, "weight": weight}


def _v1_node(
    node_id: str,
    label: str,
    weight: float,
    written_seconds: int,
    edges: list[dict[str, Any]],
) -> dict[str, Any]:
    return {
        "id": node_id,
        "label": label,
        "weight": weight,
        "written_seconds": written_seconds,
        "adjacency": {
            "targets": [edge["target"] for edge in edges],
            "labels": [edge["label"] for edge in edges],
            "weights": [edge["weight"] for edge in edges],
        },
        "out_edges": edges,
    }


def _v2_edge(target: str, label: str, weight: float) -> dict[str, Any]:
    return {"target": target, "label": label, "weight": weight}


def _v2_node(
    canonical_id: str,
    label: str | None,
    weight: float | None,
    write_ms: int,
    tombstone: bool,
    edges: list[dict[str, Any]],
) -> dict[str, Any]:
    record = {
        "canonical_id": canonical_id,
        "label": label,
        "weight": weight,
        "write_ms": write_ms,
        "tombstone": tombstone,
        "out_edges": sorted(edges, key=lambda edge: (edge["target"], edge["label"])),
    }
    record["node_digest"] = v2_node_digest(record)
    return record


def _write_v1(path: Path) -> None:
    base = 1_700_000_000_000
    records = [
        _v1_node(
            "n7",
            "person/analyst",
            1.2,
            7,
            [
                _v1_edge("n1", "follows", 0.5),
                _v1_edge("n8", "reads", 3.0),
            ],
        ),
        _v1_node(
            "n1",
            "person/engineer",
            1.5,
            1,
            [
                _v1_edge("n2", "follows", 0.5),
                _v1_edge("n3", "member", 1.0),
                _v1_edge("n4", "reads", 2.0),
            ],
        ),
        _v1_node(
            "n5",
            "topic/storage",
            2.0,
            5,
            [_v1_edge("n1", "reads", 2.0)],
        ),
        _v1_node(
            "n2",
            "person/manager",
            1.4,
            2,
            [
                _v1_edge("n1", "follows", 0.5),
                _v1_edge("n4", "reads", 1.0),
            ],
        ),
        _v1_node(
            "n9",
            "org/lab",
            1.1,
            0,
            [],
        ),
        _v1_node(
            "n4",
            "topic/db",
            2.5,
            4,
            [
                _v1_edge("n5", "mentions", 2.5),
                _v1_edge("n6", "mentions", 1.0),
            ],
        ),
        _v1_node(
            "n3",
            "org/team",
            1.3,
            3,
            [_v1_edge("n5", "owns", 0.75)],
        ),
        _v1_node(
            "n8",
            "topic/ai",
            2.2,
            8,
            [_v1_edge("n7", "mentions", 1.25)],
        ),
        _v1_node(
            "n6",
            "org/research",
            1.8,
            6,
            [_v1_edge("n2", "member", 1.0)],
        ),
    ]
    snapshot = {
        "schema": V1_SCHEMA,
        "snapshot_id": "sample-v1-base",
        "version": 1,
        "clock_epoch_ms": base,
        "nodes": records,
        "linear_index": sorted(record["id"] for record in records),
    }
    atomic_write_json(path, snapshot)


def _write_v2(root: Path) -> None:
    shard_dir = root / "shards"
    shard_dir.mkdir(parents=True, exist_ok=True)
    base = DEFAULT_LOGICAL_CLOCK_MS + 60_000
    shard_payloads = [
        {
            "shard_id": "s0001",
            "file": "shards/s0001.json",
            "generation": 1,
            "nodes": [
                _v2_node(
                    "n1",
                    "person/senior",
                    1.7,
                    base + 1_000,
                    False,
                    [
                        _v2_edge("n2", "follows", 0.8),
                        _v2_edge("n4", "reads", 2.0),
                        _v2_edge("n6", "member", 1.0),
                    ],
                ),
                _v2_node(
                    "n2",
                    "person/manager",
                    1.6,
                    base + 2_000,
                    False,
                    [
                        _v2_edge("n1", "follows", 0.5),
                        _v2_edge("n4", "reads", 1.0),
                        _v2_edge("n9", "member", 1.0),
                    ],
                ),
                _v2_node(
                    "n3",
                    None,
                    None,
                    base + 3_000,
                    True,
                    [],
                ),
                _v2_node(
                    "n10",
                    "org/new",
                    1.9,
                    base + 4_000,
                    False,
                    [_v2_edge("n1", "member", 0.4)],
                ),
            ],
        },
        {
            "shard_id": "s0002",
            "file": "shards/s0002.json",
            "generation": 2,
            "nodes": [
                _v2_node(
                    "n4",
                    "topic/database",
                    2.8,
                    base + 5_000,
                    False,
                    [
                        _v2_edge("n5", "mentions", 2.5),
                        _v2_edge("n7", "cites", 1.5),
                    ],
                ),
                _v2_node(
                    "n11",
                    "topic/sharding",
                    2.3,
                    base + 6_000,
                    False,
                    [
                        _v2_edge("n4", "mentions", 0.9),
                        _v2_edge("n10", "observes", 0.25),
                    ],
                ),
            ],
        },
    ]

    shard_meta: list[dict[str, Any]] = []
    for ordinal, payload in enumerate(shard_payloads):
        shard_document = {
            "schema": V2_SHARD_SCHEMA,
            "shard_id": payload["shard_id"],
            "generation": payload["generation"],
            "nodes": payload["nodes"],
        }
        shard_path = root / payload["file"]
        atomic_write_json(shard_path, shard_document)
        raw = shard_path.read_bytes()
        shard_meta.append(
            {
                "shard_id": payload["shard_id"],
                "file": payload["file"],
                "sha256": digest_bytes(raw),
                "generation": payload["generation"],
                "node_count": len(payload["nodes"]),
                "ordinal": ordinal,
            }
        )

    total_nodes = sum(item["node_count"] for item in shard_meta)
    manifest = {
        "schema": V2_MANIFEST_SCHEMA,
        "snapshot_id": "sample-v2-incremental",
        "version": 2,
        "logical_clock_floor": base,
        "node_count_min": total_nodes,
        "node_count_max": total_nodes,
        "shards": [
            {
                "shard_id": item["shard_id"],
                "file": item["file"],
                "sha256": item["sha256"],
                "generation": item["generation"],
                "node_count": item["node_count"],
            }
            for item in shard_meta
        ],
    }
    atomic_write_json(root / "manifest.json", manifest)

    metadata_path = root / "metadata.sqlite"
    if metadata_path.exists():
        metadata_path.unlink()
    connection = sqlite3.connect(metadata_path)
    try:
        connection.executescript(
            """
            PRAGMA journal_mode = DELETE;
            PRAGMA synchronous = FULL;
            CREATE TABLE snapshot_meta (
                id INTEGER PRIMARY KEY CHECK (id = 1),
                snapshot_id TEXT NOT NULL,
                version INTEGER NOT NULL,
                logical_clock_floor INTEGER NOT NULL,
                node_count_min INTEGER NOT NULL,
                node_count_max INTEGER NOT NULL
            );
            CREATE TABLE shards (
                shard_id TEXT PRIMARY KEY,
                file TEXT NOT NULL,
                sha256 TEXT NOT NULL,
                generation INTEGER NOT NULL,
                ordinal INTEGER NOT NULL,
                node_count INTEGER NOT NULL
            );
            CREATE TABLE nodes (
                canonical_id TEXT PRIMARY KEY,
                shard_id TEXT NOT NULL,
                shard_generation INTEGER NOT NULL,
                write_ms INTEGER NOT NULL,
                label TEXT,
                weight REAL,
                tombstone INTEGER NOT NULL,
                out_edges_json TEXT NOT NULL,
                node_digest TEXT NOT NULL
            );
            """
        )
        connection.execute(
            """
            INSERT INTO snapshot_meta(
                id, snapshot_id, version, logical_clock_floor,
                node_count_min, node_count_max
            ) VALUES (1, ?, ?, ?, ?, ?)
            """,
            (
                manifest["snapshot_id"],
                manifest["version"],
                manifest["logical_clock_floor"],
                manifest["node_count_min"],
                manifest["node_count_max"],
            ),
        )
        for item in shard_meta:
            connection.execute(
                """
                INSERT INTO shards(
                    shard_id, file, sha256, generation, ordinal, node_count
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    item["shard_id"],
                    item["file"],
                    item["sha256"],
                    item["generation"],
                    item["ordinal"],
                    item["node_count"],
                ),
            )
        for payload in shard_payloads:
            for record in payload["nodes"]:
                connection.execute(
                    """
                    INSERT INTO nodes(
                        canonical_id, shard_id, shard_generation, write_ms,
                        label, weight, tombstone, out_edges_json, node_digest
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        record["canonical_id"],
                        payload["shard_id"],
                        payload["generation"],
                        record["write_ms"],
                        record["label"],
                        record["weight"],
                        1 if record["tombstone"] else 0,
                        canonical_json(record["out_edges"]),
                        record["node_digest"],
                    ),
                )
        connection.commit()
    finally:
        connection.close()


def generate_samples(root: str | Path, *, force: bool = False) -> dict[str, Any]:
    destination = Path(root)
    if destination.exists():
        if not force:
            raise FileExistsError(
                f"{destination} already exists; pass force=True to replace it"
            )
        shutil.rmtree(destination)
    v1_dir = destination / "v1"
    v2_dir = destination / "v2"
    v1_dir.mkdir(parents=True)
    v2_dir.mkdir(parents=True)
    v1_path = v1_dir / "snapshot.json"
    _write_v1(v1_path)
    _write_v2(v2_dir)

    readme = f"""# Deterministic graph samples

Generated by `graphstore.samples`.

- v1: `{v1_path.relative_to(destination)}`
- v2: `{v2_dir.relative_to(destination)}`
- default injected v1 logical clock: `{DEFAULT_LOGICAL_CLOCK_MS}` ms
- v1 scale: `{SECOND}` ms/second, rounding: `half_up`

`n1 -> n2/follows` deliberately conflicts between v1 (weight 0.5) and v2
(weight 0.8). `n3` is tombstoned in v2 and must remain audit-only. v2 has two
shards with generations 1 and 2, each hash and node digest is precomputed.
"""
    (destination / "README.md").write_text(readme, encoding="utf-8")
    return {
        "root": str(destination),
        "v1": str(v1_path),
        "v2": str(v2_dir),
        "logical_clock_ms": DEFAULT_LOGICAL_CLOCK_MS,
        "v1_clock_scale_ms": SECOND,
        "v1_rounding": "half_up",
        "package_schema": PACKAGE_SCHEMA,
    }

