from __future__ import annotations

import json
import sqlite3
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from graphstore.checksums import digest_edges, sha256_file, sha256_object
from graphstore.merge import merge_snapshots
from graphstore.model import ClockConfig
from graphstore.validation import validate_v1_snapshot, validate_v2_snapshot


CLOCK = ClockConfig(
    anchor_ms=1_710_000_000_000,
    step_ms=250,
    max_rollback_ms=1_000,
    v1_scale_numerator=1_000,
    v1_scale_denominator=1,
    v1_rounding="floor",
)


def write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            value,
            ensure_ascii=False,
            allow_nan=False,
            indent=2,
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )


def v1_node(
    node_id: str,
    label: str,
    edges: list[tuple[str, str]],
    weight: float,
    relative_seconds: int,
) -> dict[str, Any]:
    adjacency: list[str] = []
    out_edges: list[dict[str, str]] = []
    for target, edge_label in edges:
        adjacency.extend([target, edge_label])
        out_edges.append({"label": edge_label, "target": target})
    return {
        "adjacency": adjacency,
        "id": node_id,
        "label": label,
        "out_edges": out_edges,
        "relative_write_seconds": relative_seconds,
        "weight": weight,
    }


def build_v1(base: Path) -> Path:
    nodes = [
        v1_node(
            "n1",
            "person",
            [("n2", "follows"), ("n3", "friend")],
            1.5,
            1,
        ),
        v1_node(
            "n2",
            "person",
            [("n3", "follows"), ("n4", "blocked")],
            2.0,
            2,
        ),
        v1_node("n3", "person", [("n5", "follows")], 1.25, 3),
        v1_node("n5", "service", [], 0.5, 4),
        v1_node("n6", "person", [("n1", "likes")], 3.0, 5),
        v1_node("n7", "muted", [], 1.0, 6),
    ]
    document = {
        "format": "v1",
        "linear_index": sorted(node["id"] for node in nodes),
        "logical_clock_ms": 1_710_000_002_000,
        "nodes": nodes,
        "snapshot_version": 2,
    }
    path = base / "v1" / "snapshot.json"
    write_json(path, document)
    return path


def v2_node(
    node_id: str,
    label: str,
    edges: list[tuple[str, str, float]],
    written_at_ms: int,
    generation: int,
    tombstone: bool = False,
) -> dict[str, Any]:
    return {
        "canonical_id": node_id,
        "generation": generation,
        "label": label,
        "out_edges": [
            {"label": edge_label, "target": target, "weight": weight}
            for target, edge_label, weight in edges
        ],
        "state": "tombstone" if tombstone else "active",
        "tombstone": tombstone,
        "written_at_ms": written_at_ms,
    }


def build_v2(base: Path) -> Path:
    shard_documents = [
        {
            "format": "v2-shard",
            "generation": 4,
            "nodes": [
                v2_node(
                    "n1",
                    "person",
                    [
                        ("n2", "follows", 0.8),
                        ("n3", "friend", 0.4),
                        ("n7", "likes", 0.2),
                    ],
                    1_710_000_004_500,
                    4,
                ),
                v2_node(
                    "n2",
                    "person",
                    [("n4", "follows", 0.6)],
                    1_710_000_004_600,
                    4,
                ),
                v2_node(
                    "n3",
                    "person",
                    [],
                    1_710_000_005_500,
                    4,
                    tombstone=True,
                ),
            ],
            "shard_id": "shard-000",
        },
        {
            "format": "v2-shard",
            "generation": 5,
            "nodes": [
                v2_node(
                    "n4",
                    "auditor",
                    [("n5", "blocked", 0.9)],
                    1_710_000_005_100,
                    5,
                ),
                v2_node(
                    "n5",
                    "service",
                    [("n8", "follows", 0.7)],
                    1_710_000_005_200,
                    5,
                ),
                v2_node(
                    "n8",
                    "system",
                    [],
                    1_710_000_005_300,
                    5,
                ),
                v2_node(
                    "n9",
                    "person",
                    [],
                    1_710_000_005_400,
                    5,
                    tombstone=True,
                ),
            ],
            "shard_id": "shard-001",
        },
    ]
    shard_metadata: list[dict[str, Any]] = []
    all_edges: list[tuple[str, str, str, float]] = []
    shard_entries: list[dict[str, Any]] = []
    for ordinal, document in enumerate(shard_documents):
        content_hash = sha256_object(document)
        document["content_hash"] = content_hash
        shard_path = base / "v2" / "shards" / f"{document['shard_id']}.json"
        write_json(shard_path, document)
        shard_edges = [
            (
                node["canonical_id"],
                edge["target"],
                edge["label"],
                float(edge["weight"]),
            )
            for node in document["nodes"]
            for edge in node["out_edges"]
        ]
        all_edges.extend(shard_edges)
        shard_metadata.append(
            {
                "adjacency_digest": digest_edges(shard_edges),
                "content_hash": content_hash,
                "edge_count": len(shard_edges),
                "generation": document["generation"],
                "node_count": len(document["nodes"]),
                "ordinal": ordinal,
                "shard_id": document["shard_id"],
            }
        )
        shard_entries.append(
            {
                "content_hash": content_hash,
                "generation": document["generation"],
                "path": f"shards/{document['shard_id']}.json",
                "shard_id": document["shard_id"],
            }
        )
    adjacency_digest = digest_edges(all_edges)
    manifest = {
        "adjacency_digest": adjacency_digest,
        "format": "v2",
        "logical_clock_ms": 1_710_000_006_000,
        "metadata_db": "meta.sqlite",
        "shards": shard_entries,
        "snapshot_version": 3,
    }
    manifest_path = base / "v2" / "manifest.json"
    write_json(manifest_path, manifest)

    metadata_path = base / "v2" / "meta.sqlite"
    if metadata_path.exists():
        metadata_path.unlink()
    connection = sqlite3.connect(metadata_path)
    try:
        connection.executescript(
            """
            CREATE TABLE meta (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            );
            CREATE TABLE shards (
                ordinal INTEGER PRIMARY KEY,
                shard_id TEXT NOT NULL UNIQUE,
                generation INTEGER NOT NULL,
                content_hash TEXT NOT NULL,
                node_count INTEGER NOT NULL,
                edge_count INTEGER NOT NULL,
                adjacency_digest TEXT NOT NULL
            );
            """
        )
        connection.executemany(
            "INSERT INTO meta(key, value) VALUES (?, ?)",
            [
                ("adjacency_digest", adjacency_digest),
                ("format", "v2"),
                ("global_generation", "5"),
                ("logical_clock_ms", "1710000006000"),
                ("snapshot_version", "3"),
            ],
        )
        connection.executemany(
            """
            INSERT INTO shards(
                ordinal, shard_id, generation, content_hash, node_count,
                edge_count, adjacency_digest
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            [
                (
                    row["ordinal"],
                    row["shard_id"],
                    row["generation"],
                    row["content_hash"],
                    row["node_count"],
                    row["edge_count"],
                    row["adjacency_digest"],
                )
                for row in shard_metadata
            ],
        )
        connection.commit()
    finally:
        connection.close()
    return manifest_path


def build_append_v1(base: Path) -> Path:
    original = json.loads((base / "v1" / "snapshot.json").read_text(encoding="utf-8"))
    nodes = list(original["nodes"])
    nodes.append(
        v1_node("n10", "person", [("n6", "follows")], 0.75, 7)
    )
    document = {
        "format": "v1",
        "linear_index": sorted(node["id"] for node in nodes),
        "logical_clock_ms": 1_710_000_003_000,
        "nodes": nodes,
        "snapshot_version": 4,
    }
    path = base / "packages" / "append" / "v1.json"
    write_json(path, document)
    return path


def package_manifest(
    package_id: str,
    package_version: int,
    package_clock_ms: int,
    change_kind: str,
    v1_path: Path,
    v2_manifest_path: Path,
    v2_metadata_path: Path,
    package_dir: Path,
    node_bounds: tuple[int, int],
) -> dict[str, Any]:
    v1 = validate_v1_snapshot(v1_path)
    v2 = validate_v2_snapshot(v2_manifest_path)
    merged = merge_snapshots(v1, v2, CLOCK)
    package_dir = package_dir.resolve()

    def relative(path: Path) -> str:
        return (
            "../" * len(package_dir.relative_to(ROOT).parts)
            + path.resolve().relative_to(ROOT).as_posix()
        )

    shard_entries = []
    for shard in v2.shards:
        shard_entries.append(
            {
                "path": relative(Path(shard.path)),
                "sha256": shard.file_sha256,
                "shard_id": shard.shard_id,
            }
        )
    return {
        "adjacency_digest": merged.adjacency_digest(),
        "change_kind": change_kind,
        "logical_clock_ms": package_clock_ms,
        "node_count_bounds": {"max": node_bounds[1], "min": node_bounds[0]},
        "package_id": package_id,
        "package_version": package_version,
        "schema": "graph-package/v1",
        "shards": shard_entries,
        "sources": {
            "v1": {
                "path": relative(v1_path),
                "sha256": sha256_file(v1_path),
            },
            "v2": {
                "manifest": relative(v2_manifest_path),
                "metadata_db": relative(v2_metadata_path),
                "metadata_sha256": sha256_file(v2_metadata_path),
                "sha256": sha256_file(v2_manifest_path),
            },
        },
    }


def write_package(path: Path, manifest: dict[str, Any]) -> None:
    write_json(path, manifest)


def build_packages(base: Path) -> None:
    v1_path = base / "v1" / "snapshot.json"
    v2_manifest_path = base / "v2" / "manifest.json"
    v2_metadata_path = base / "v2" / "meta.sqlite"

    base_package_dir = base / "packages" / "base"
    base_manifest = package_manifest(
        "base",
        3,
        1_710_000_100_000,
        "full",
        v1_path,
        v2_manifest_path,
        v2_metadata_path,
        base_package_dir,
        (9, 9),
    )
    write_package(base_package_dir / "manifest.json", base_manifest)

    rollback_dir = base / "packages" / "rollback"
    rollback_manifest = package_manifest(
        "rollback",
        1,
        1_710_000_100_000,
        "full",
        v1_path,
        v2_manifest_path,
        v2_metadata_path,
        rollback_dir,
        (9, 9),
    )
    write_package(rollback_dir / "manifest.json", rollback_manifest)

    clock_rollback_dir = base / "packages" / "clock-rollback"
    clock_rollback_manifest = package_manifest(
        "clock-rollback",
        5,
        1_710_000_006_000,
        "full",
        v1_path,
        v2_manifest_path,
        v2_metadata_path,
        clock_rollback_dir,
        (9, 9),
    )
    write_package(clock_rollback_dir / "manifest.json", clock_rollback_manifest)

    append_v1 = build_append_v1(base)
    append_dir = base / "packages" / "append"
    append_manifest = package_manifest(
        "base",
        4,
        1_710_000_200_000,
        "append",
        append_v1,
        v2_manifest_path,
        v2_metadata_path,
        append_dir,
        (9, 10),
    )
    write_package(append_dir / "manifest.json", append_manifest)

    partial_dir = base / "packages" / "partial"
    partial_manifest = package_manifest(
        "partial",
        4,
        1_710_000_300_000,
        "full",
        v1_path,
        v2_manifest_path,
        v2_metadata_path,
        partial_dir,
        (9, 9),
    )
    partial_manifest["shards"][0]["sha256"] = "0" * 64
    write_package(partial_dir / "manifest.json", partial_manifest)

    missing_dir = base / "packages" / "missing"
    missing_manifest = package_manifest(
        "missing",
        4,
        1_710_000_400_000,
        "full",
        v1_path,
        v2_manifest_path,
        v2_metadata_path,
        missing_dir,
        (9, 9),
    )
    missing_manifest["shards"][0]["path"] = str(
        Path(missing_manifest["shards"][0]["path"]).with_name("not-present.json")
    )
    write_package(missing_dir / "manifest.json", missing_manifest)


def build_injection_manifest(base: Path) -> None:
    document = {
        "cases": [
            {
                "command": "load",
                "expected": "commit",
                "name": "base_load",
                "package": "samples/packages/base/manifest.json",
            },
            {
                "command": "replace",
                "expected_error": "GS_VERSION_ROLLBACK",
                "name": "version_rollback_rejected",
                "package": "samples/packages/rollback/manifest.json",
            },
            {
                "command": "replace",
                "expected_error": "GS_CLOCK_ROLLBACK",
                "name": "clock_rollback_rejected",
                "package": "samples/packages/clock-rollback/manifest.json",
            },
            {
                "command": "replace",
                "expected_error": "GS_SHARD_HASH_MISMATCH",
                "name": "bad_shard_hash_rejected",
                "package": "samples/packages/partial/manifest.json",
            },
            {
                "command": "replace",
                "expected_error": "GS_MISSING_FILE",
                "name": "missing_shard_rejected",
                "package": "samples/packages/missing/manifest.json",
            },
            {
                "command": "traverse",
                "crash_at": "traverse_after_layer_1",
                "name": "crash_resume_two_hop",
                "query": {
                    "batch_id": "two-hop",
                    "depth": 2,
                    "kind": "reachable",
                    "node_id": "n1",
                },
            },
        ],
        "clock_file": "samples/clock.json",
        "schema": "injection-manifest/v1",
        "workspace_relative_paths": True,
    }
    write_json(base / "injection.json", document)


def build_clock(base: Path) -> None:
    write_json(
        base / "clock.json",
        {
            "anchor_ms": CLOCK.anchor_ms,
            "max_rollback_ms": CLOCK.max_rollback_ms,
            "schema": "logical-clock/v1",
            "step_ms": CLOCK.step_ms,
            "v1_conversion": {
                "rounding": CLOCK.v1_rounding,
                "scale": {
                    "denominator": CLOCK.v1_scale_denominator,
                    "numerator": CLOCK.v1_scale_numerator,
                },
            },
        },
    )


def main() -> None:
    build_clock(ROOT / "samples")
    build_v1(ROOT / "samples")
    build_v2(ROOT / "samples")
    build_packages(ROOT / "samples")
    build_injection_manifest(ROOT / "samples")
    print("samples generated")


if __name__ == "__main__":
    main()

