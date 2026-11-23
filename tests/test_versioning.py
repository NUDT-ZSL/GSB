from __future__ import annotations

import json
import shutil
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from lbworkbench.db import WorkbenchDB, connect
from lbworkbench.errors import WorkbenchError
from lbworkbench.store import get_status, list_snapshots, load_package, verify_state
from lbworkbench.util import sha256_file
from lbworkbench.util import sha256_json

from conftest import read_json, write_json


def _retag_package(sample_dir: Path, *, new_version: str) -> Path:
    source = sample_dir / "package-2026.11.1.json"
    package = read_json(source)
    meta_path = sample_dir / package["v2"]["meta_db"]
    conn = sqlite3.connect(meta_path)
    try:
        conn.execute(
            "UPDATE snapshot_meta SET package_version = ?", (new_version,)
        )
        conn.commit()
    finally:
        conn.close()
    package["package_version"] = new_version
    package["v2"]["meta_sha256"] = sha256_file(meta_path)
    v1_path = sample_dir / package["v1"]["path"]
    v1 = read_json(v1_path)
    v1["instances"].append(
        {
            "instance_id": "INC-01",
            "relative_failure_seconds": 0,
            "consecutive_failures": 0,
            "status": "healthy",
        }
    )
    write_json(v1_path, v1)
    package["v1"]["sha256"] = sha256_file(v1_path)
    package["instance_set_sha256"] = sha256_json(
        sorted(
            {item["instance_id"].lower() for item in v1["instances"]}
            | {
                item["canonical_instance_id"]
                for shard in read_json(sample_dir / package["v2"]["manifest"])["shards"]
                for item in read_json(
                    (sample_dir / package["v2"]["manifest"]).parent / shard["path"]
                )["instances"]
            }
        )
    )
    target = sample_dir / f"package-{new_version}.json"
    write_json(target, package)
    return target


def test_noop_incremental_replacement_and_rollback(
    sample_copy: Path,
    tmp_path: Path,
) -> None:
    db = WorkbenchDB(tmp_path / "work")
    baseline = sample_copy / "package-2026.11.1.json"
    first = load_package(db, baseline)
    assert first["result"] == "full_replacement"
    noop = load_package(db, baseline)
    assert noop["result"] == "no_op"

    version_copy = tmp_path / "versions"
    shutil.copytree(sample_copy, version_copy)
    incremental_package = _retag_package(version_copy, new_version="2026.11.1.1")
    incremental = load_package(db, incremental_package)
    assert incremental["result"] == "incremental_append"

    replacement = load_package(db, sample_copy / "package-2026.11.2.json")
    assert replacement["result"] == "full_replacement"
    assert get_status(db)["current_snapshot"]["version"] == "2026.11.2"
    assert verify_state(db)["ok"] is True

    with pytest.raises(WorkbenchError) as caught:
        load_package(db, baseline)
    assert caught.value.code == "E_VERSION_ROLLBACK"
    assert get_status(db)["current_snapshot"]["version"] == "2026.11.2"


def test_same_version_different_content_is_conflict(
    sample_copy: Path,
    tmp_path: Path,
) -> None:
    db = WorkbenchDB(tmp_path / "work")
    baseline = sample_copy / "package-2026.11.1.json"
    load_package(db, baseline)
    package = read_json(baseline)
    package["instance_set_sha256"] = "f" * 64
    write_json(baseline, package)
    with pytest.raises(WorkbenchError) as caught:
        load_package(db, baseline)
    assert caught.value.code in {"E_HASH_MISMATCH", "E_VERSION_CONFLICT"}


def test_reader_snapshot_isolation_during_replacement(
    sample_copy: Path,
    tmp_path: Path,
) -> None:
    work = tmp_path / "work"
    db = WorkbenchDB(work)
    load_package(db, sample_copy / "package-2026.11.1.json")

    reader = connect(db.db_path)
    reader.execute("BEGIN")
    try:
        old_version = reader.execute(
            "SELECT value FROM meta WHERE key = 'current_snapshot_version'"
        ).fetchone()["value"]
        old_instances = {
            row["instance_id"]
            for row in reader.execute(
                "SELECT instance_id FROM merged_instances WHERE snapshot_version = ?",
                (old_version,),
            )
        }
        load_package(db, sample_copy / "package-2026.11.2.json")
        visible_old_version = reader.execute(
            "SELECT value FROM meta WHERE key = 'current_snapshot_version'"
        ).fetchone()["value"]
        visible_old_instances = {
            row["instance_id"]
            for row in reader.execute(
                "SELECT instance_id FROM merged_instances WHERE snapshot_version = ?",
                (visible_old_version,),
            )
        }
        assert visible_old_version == "2026.11.1"
        assert visible_old_instances == old_instances
    finally:
        reader.execute("COMMIT")
        reader.close()

    status = get_status(db)
    new_ids = {item["instance_id"] for item in status["instances"]}
    assert status["current_snapshot"]["version"] == "2026.11.2"
    assert "edge-02" in new_ids and "edge-01" not in new_ids
    version_rows = {
        row["snapshot_version"]
        for row in connect(db.db_path).execute(
            "SELECT DISTINCT snapshot_version FROM instance_state"
        )
    }
    assert version_rows == {"2026.11.1", "2026.11.2"}


def test_concurrent_same_package_load_is_idempotent(
    sample_copy: Path,
    tmp_path: Path,
) -> None:
    work = tmp_path / "work"
    WorkbenchDB(work).initialize()
    package = sample_copy / "package-2026.11.1.json"

    def load_once() -> dict:
        return load_package(WorkbenchDB(work), package)

    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(lambda _: load_once(), range(4)))
    kinds = [result["result"] for result in results]
    assert kinds.count("full_replacement") == 1
    assert kinds.count("no_op") == 3
    snaps = list_snapshots(WorkbenchDB(work))
    assert len(snaps["snapshots"]) == 1
    assert snaps["current_snapshot_version"] == "2026.11.1"
