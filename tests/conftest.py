from __future__ import annotations

import json
import shutil
import sqlite3
from pathlib import Path
from typing import Any

import pytest

from graphstore.samples import DEFAULT_LOGICAL_CLOCK_MS, generate_samples
from graphstore.snapshot import v2_node_digest
from graphstore.util import atomic_write_json, canonical_json, digest_bytes


@pytest.fixture(scope="session")
def pristine_samples(tmp_path_factory: pytest.TempPathFactory) -> Path:
    root = tmp_path_factory.mktemp("pristine-samples") / "samples"
    generate_samples(root)
    return root


@pytest.fixture
def sample_copy(tmp_path: Path, pristine_samples: Path) -> Path:
    destination = tmp_path / "samples"
    shutil.copytree(pristine_samples, destination)
    return destination


@pytest.fixture
def sample_paths(sample_copy: Path) -> dict[str, Any]:
    return {
        "root": sample_copy,
        "v1": sample_copy / "v1" / "snapshot.json",
        "v2": sample_copy / "v2",
        "logical_clock_ms": DEFAULT_LOGICAL_CLOCK_MS,
    }


def load_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def write_json(path: Path, value: Any) -> None:
    atomic_write_json(path, value)


def update_shard_hashes(v2_root: Path, shard_ids: set[str] | None = None) -> dict[str, str]:
    manifest_path = v2_root / "manifest.json"
    manifest = load_json(manifest_path)
    hashes: dict[str, str] = {}
    for shard in manifest["shards"]:
        if shard_ids is not None and shard["shard_id"] not in shard_ids:
            continue
        data = (v2_root / shard["file"]).read_bytes()
        shard["sha256"] = digest_bytes(data)
        hashes[shard["shard_id"]] = shard["sha256"]
    write_json(manifest_path, manifest)
    connection = sqlite3.connect(v2_root / "metadata.sqlite")
    try:
        for shard_id, digest in hashes.items():
            connection.execute(
                "UPDATE shards SET sha256 = ? WHERE shard_id = ?",
                (digest, shard_id),
            )
        connection.commit()
    finally:
        connection.close()
    return hashes


def write_shard(v2_root: Path, shard_id: str) -> Path:
    manifest = load_json(v2_root / "manifest.json")
    shard = next(
        item for item in manifest["shards"] if item["shard_id"] == shard_id
    )
    return v2_root / shard["file"]


def mutate_v2_manifest_and_metadata(
    v2_root: Path, updates: dict[str, Any]
) -> None:
    manifest_path = v2_root / "manifest.json"
    manifest = load_json(manifest_path)
    manifest.update(updates)
    write_json(manifest_path, manifest)
    connection = sqlite3.connect(v2_root / "metadata.sqlite")
    try:
        assignments = ", ".join(f"{key} = ?" for key in updates)
        connection.execute(
            f"UPDATE snapshot_meta SET {assignments} WHERE id = 1",
            tuple(updates.values()),
        )
        connection.commit()
    finally:
        connection.close()


def update_v2_node(
    v2_root: Path,
    node_id: str,
    *,
    write_ms: Any | None = None,
    node_digest: str | None = None,
) -> None:
    manifest = load_json(v2_root / "manifest.json")
    for shard_meta in manifest["shards"]:
        shard_path = v2_root / shard_meta["file"]
        shard = load_json(shard_path)
        changed = False
        for record in shard["nodes"]:
            if record["canonical_id"] != node_id:
                continue
            if write_ms is not None:
                record["write_ms"] = write_ms
                record["node_digest"] = v2_node_digest(
                    {
                        "canonical_id": record["canonical_id"],
                        "label": record["label"],
                        "weight": record["weight"],
                        "write_ms": record["write_ms"],
                        "tombstone": record["tombstone"],
                        "out_edges": record["out_edges"],
                    }
                )
            if node_digest is not None:
                record["node_digest"] = node_digest
            changed = True
        if changed:
            write_json(shard_path, shard)
            update_shard_hashes(v2_root, {shard_meta["shard_id"]})
            return
    raise AssertionError(f"node {node_id} not found")

