from __future__ import annotations

import json
import shutil
import sqlite3
from pathlib import Path

import pytest

from graphstore.errors import (
    E_PACKAGE_NODE_BOUNDS,
    E_SHARD_HASH_MISMATCH,
    E_V1_ADJACENCY_MISMATCH,
    E_V1_DUPLICATE_NODE_ID,
    E_V1_INVALID_RELATIVE_TIME,
    E_V1_INVALID_WEIGHT,
    E_V1_LINEAR_INDEX,
    GraphStoreError,
)
from graphstore.checksums import sha256_object
from graphstore.model import ClockConfig
from graphstore.package import load_package
from graphstore.storage import GraphStore
from graphstore.validation import validate_v1_snapshot
from graphstore.validation import validate_v2_snapshot

from conftest import ROOT, write_json


def _v1_document() -> dict:
    return json.loads((ROOT / "samples/v1/snapshot.json").read_text(encoding="utf-8"))


def test_v1_rejects_duplicate_ids(tmp_path: Path) -> None:
    document = _v1_document()
    document["nodes"].append(dict(document["nodes"][0]))
    path = tmp_path / "v1.json"
    write_json(path, document)
    with pytest.raises(GraphStoreError) as error:
        validate_v1_snapshot(path)
    assert error.value.code == E_V1_DUPLICATE_NODE_ID


def test_v1_rejects_adjacency_disagreement(tmp_path: Path) -> None:
    document = _v1_document()
    document["nodes"][0]["adjacency"][1] = "different"
    path = tmp_path / "v1.json"
    write_json(path, document)
    with pytest.raises(GraphStoreError) as error:
        validate_v1_snapshot(path)
    assert error.value.code == E_V1_ADJACENCY_MISMATCH


def test_v1_rejects_bad_weight_and_relative_time(tmp_path: Path) -> None:
    document = _v1_document()
    document["nodes"][0]["weight"] = "heavy"
    first = tmp_path / "weight.json"
    write_json(first, document)
    with pytest.raises(GraphStoreError) as error:
        validate_v1_snapshot(first)
    assert error.value.code == E_V1_INVALID_WEIGHT

    document = _v1_document()
    document["nodes"][0]["relative_write_seconds"] = True
    second = tmp_path / "time.json"
    write_json(second, document)
    with pytest.raises(GraphStoreError) as error:
        validate_v1_snapshot(second)
    assert error.value.code == E_V1_INVALID_RELATIVE_TIME


def test_v1_rejects_bad_linear_index(tmp_path: Path) -> None:
    document = _v1_document()
    document["linear_index"] = list(reversed(document["linear_index"]))
    path = tmp_path / "v1.json"
    write_json(path, document)
    with pytest.raises(GraphStoreError) as error:
        validate_v1_snapshot(path)
    assert error.value.code == E_V1_LINEAR_INDEX


def test_v2_rejects_content_hash_mismatch(tmp_path: Path) -> None:
    copied = tmp_path / "v2"
    shutil.copytree(ROOT / "samples/v2", copied)
    shard_path = copied / "shards/shard-000.json"
    shard = json.loads(shard_path.read_text(encoding="utf-8"))
    shard["nodes"][0]["label"] = "tampered"
    write_json(shard_path, shard)
    with pytest.raises(GraphStoreError) as error:
        validate_v2_snapshot(copied / "manifest.json")
    assert error.value.code == E_SHARD_HASH_MISMATCH


def test_v2_rejects_inconsistent_tombstone_state(tmp_path: Path) -> None:
    copied = tmp_path / "v2"
    shutil.copytree(ROOT / "samples/v2", copied)
    shard_path = copied / "shards/shard-000.json"
    shard = json.loads(shard_path.read_text(encoding="utf-8"))
    shard["nodes"][0]["state"] = "tombstone"
    shard.pop("content_hash", None)
    content_hash = sha256_object(shard)
    shard["content_hash"] = content_hash
    write_json(shard_path, shard)

    manifest_path = copied / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["shards"][0]["content_hash"] = content_hash
    write_json(manifest_path, manifest)
    with sqlite3.connect(copied / "meta.sqlite") as connection:
        connection.execute(
            "UPDATE shards SET content_hash = ? WHERE shard_id = 'shard-000'",
            (content_hash,),
        )
        connection.commit()

    with pytest.raises(GraphStoreError) as error:
        validate_v2_snapshot(manifest_path)
    assert error.value.code == "GS_V2_TOMBSTONE"


@pytest.mark.parametrize(
    ("package", "code"),
    [
        ("samples/packages/partial/manifest.json", E_SHARD_HASH_MISMATCH),
        ("samples/packages/missing/manifest.json", "GS_MISSING_FILE"),
    ],
)
def test_invalid_package_does_not_create_state(
    package: str, code: str, tmp_path: Path, clock: ClockConfig
) -> None:
    db_path = tmp_path / "never-created.sqlite3"
    with pytest.raises(GraphStoreError) as error:
        load_package(ROOT / package, clock)
    assert error.value.code == code
    assert not db_path.exists()


def test_node_count_bounds_are_checked(
    tmp_path: Path, clock: ClockConfig
) -> None:
    manifest = json.loads(
        (ROOT / "samples/packages/base/manifest.json").read_text(encoding="utf-8")
    )
    manifest["node_count_bounds"] = {"min": 100, "max": 200}
    manifest_path = tmp_path / "manifest.json"
    write_json(manifest_path, manifest)
    source_dir = ROOT / "samples/packages/base"
    for key in ("sources", "shards"):
        if key == "sources":
            for entry in manifest[key].values():
                for path_key in ("path", "manifest", "metadata_db"):
                    if path_key in entry:
                        entry[path_key] = str(
                            (source_dir / entry[path_key]).resolve()
                        )
        else:
            for entry in manifest[key]:
                entry["path"] = str((source_dir / entry["path"]).resolve())
    write_json(manifest_path, manifest)
    with pytest.raises(GraphStoreError) as error:
        load_package(manifest_path, clock)
    assert error.value.code == E_PACKAGE_NODE_BOUNDS

