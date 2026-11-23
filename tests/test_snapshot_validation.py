from __future__ import annotations

import json
import shutil
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

from lbworkbench.db import WorkbenchDB
from lbworkbench.errors import WorkbenchError
from lbworkbench.snapshots import parse_package
from lbworkbench.store import get_status, load_package
from lbworkbench.util import canonical_json, sha256_file

from conftest import ROOT, read_json, write_json


def _rehash_v1(sample_dir: Path) -> None:
    package_path = sample_dir / "package-2026.11.1.json"
    package = read_json(package_path)
    package["v1"]["sha256"] = sha256_file(sample_dir / package["v1"]["path"])
    write_json(package_path, package)


def _rehash_v2_shard(sample_dir: Path, shard_relative: str) -> None:
    package_path = sample_dir / "package-2026.11.1.json"
    package = read_json(package_path)
    manifest_path = sample_dir / package["v2"]["manifest"]
    manifest = read_json(manifest_path)
    manifest_root = manifest_path.parent
    for shard in manifest["shards"]:
        if shard["path"] == shard_relative:
            shard["sha256"] = sha256_file(manifest_root / shard_relative)
    write_json(manifest_path, manifest)
    package["v2"]["manifest_sha256"] = sha256_file(manifest_path)
    write_json(package_path, package)


def test_baseline_package_parses_all_formats(samples: Path) -> None:
    snapshot = parse_package(samples / "package-2026.11.1.json")
    assert snapshot["package_version"] == "2026.11.1"
    assert len(snapshot["instance_ids"]) == 8
    assert len(snapshot["v1"]["instances"]) == 7
    assert len(snapshot["v2"]["instances"]) == 7
    assert snapshot["v2"]["state_generation_floor"] == 10
    assert snapshot["instance_set_sha256"].startswith("c18d6eaf")


def test_sample_generator_is_byte_deterministic(tmp_path: Path) -> None:
    first = tmp_path / "first"
    second = tmp_path / "second"
    for output in (first, second):
        subprocess.run(
            [
                sys.executable,
                str(ROOT / "samples" / "generate_samples.py"),
                "--out",
                str(output),
            ],
            check=True,
            cwd=ROOT,
        )
    first_files = {
        path.relative_to(first): path.read_bytes()
        for path in first.rglob("*")
        if path.is_file()
    }
    second_files = {
        path.relative_to(second): path.read_bytes()
        for path in second.rglob("*")
        if path.is_file()
    }
    assert first_files == second_files


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("relative_failure_seconds", -1),
        ("consecutive_failures", 99),
        ("status", "green"),
    ],
)
def test_invalid_v1_returns_bad_snapshot_without_state_change(
    sample_copy: Path,
    tmp_path: Path,
    field: str,
    value: object,
) -> None:
    db = WorkbenchDB(tmp_path / "work")
    load_package(db, sample_copy / "package-2026.11.1.json")
    before = canonical_json(get_status(db))

    v1_path = sample_copy / "v1" / "snapshot.json"
    v1 = read_json(v1_path)
    v1["instances"][0][field] = value
    write_json(v1_path, v1)
    _rehash_v1(sample_copy)

    with pytest.raises(WorkbenchError) as caught:
        load_package(db, sample_copy / "package-2026.11.1.json")
    assert caught.value.code == "E_BAD_SNAPSHOT"
    assert canonical_json(get_status(db)) == before


def test_duplicate_v1_instance_is_rejected(sample_copy: Path, tmp_path: Path) -> None:
    v1_path = sample_copy / "v1" / "snapshot.json"
    v1 = read_json(v1_path)
    v1["instances"].append(dict(v1["instances"][0]))
    write_json(v1_path, v1)
    _rehash_v1(sample_copy)
    with pytest.raises(WorkbenchError) as caught:
        parse_package(sample_copy / "package-2026.11.1.json")
    assert caught.value.code == "E_BAD_SNAPSHOT"
    assert "duplicate" in caught.value.message


def test_shard_hash_mismatch_is_distinct_error(sample_copy: Path, tmp_path: Path) -> None:
    db = WorkbenchDB(tmp_path / "work")
    load_package(db, sample_copy / "package-2026.11.1.json")
    before = canonical_json(get_status(db))
    shard_path = sample_copy / "v2" / "shards" / "shard-000.json"
    shard = read_json(shard_path)
    shard["instances"][0]["effective_weight"] = 77
    write_json(shard_path, shard)
    with pytest.raises(WorkbenchError) as caught:
        load_package(db, sample_copy / "package-2026.11.1.json")
    assert caught.value.code == "E_HASH_MISMATCH"
    assert canonical_json(get_status(db)) == before


def test_tombstone_must_be_consistent(sample_copy: Path) -> None:
    shard_relative = "shards/shard-001.json"
    shard_path = sample_copy / "v2" / shard_relative
    shard = read_json(shard_path)
    worker = next(
        item for item in shard["instances"] if item["canonical_instance_id"] == "worker-01"
    )
    worker["absolute_failure_ms"] = 1_764_000_001_000
    write_json(shard_path, shard)
    _rehash_v2_shard(sample_copy, shard_relative)
    with pytest.raises(WorkbenchError) as caught:
        parse_package(sample_copy / "package-2026.11.1.json")
    assert caught.value.code == "E_BAD_SNAPSHOT"
    assert "tombstone" in caught.value.message


def test_v2_generation_must_be_monotonic(sample_copy: Path) -> None:
    shard_relative = "shards/shard-000.json"
    shard_path = sample_copy / "v2" / shard_relative
    shard = read_json(shard_path)
    shard["instances"][-1]["state_generation"] = 9
    write_json(shard_path, shard)
    _rehash_v2_shard(sample_copy, shard_relative)
    with pytest.raises(WorkbenchError) as caught:
        parse_package(sample_copy / "package-2026.11.1.json")
    assert caught.value.code == "E_BAD_SNAPSHOT"
    assert "generation" in caught.value.message


def test_package_instance_digest_mismatch(sample_copy: Path) -> None:
    package_path = sample_copy / "package-2026.11.1.json"
    package = read_json(package_path)
    package["instance_set_sha256"] = "0" * 64
    write_json(package_path, package)
    with pytest.raises(WorkbenchError) as caught:
        parse_package(package_path)
    assert caught.value.code == "E_HASH_MISMATCH"


def test_v2_meta_must_match_manifest(sample_copy: Path) -> None:
    package = read_json(sample_copy / "package-2026.11.1.json")
    meta_path = sample_copy / package["v2"]["meta_db"]
    conn = sqlite3.connect(meta_path)
    try:
        conn.execute(
            "UPDATE snapshot_meta SET logical_clock_ms = logical_clock_ms + 1"
        )
        conn.commit()
    finally:
        conn.close()
    package["v2"]["meta_sha256"] = sha256_file(meta_path)
    write_json(sample_copy / "package-2026.11.1.json", package)
    with pytest.raises(WorkbenchError) as caught:
        parse_package(sample_copy / "package-2026.11.1.json")
    assert caught.value.code == "E_BAD_SNAPSHOT"
    assert "metadata" in caught.value.message
