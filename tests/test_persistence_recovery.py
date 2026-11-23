from __future__ import annotations

import json
import os
import signal
import shutil
import sqlite3
import subprocess
import sys
import time
from pathlib import Path

import pytest

from conftest import (
    load_json,
    mutate_v2_manifest_and_metadata,
    update_shard_hashes,
    write_json,
)
from graphstore import GraphStore
from graphstore.errors import ErrorCode, GraphStoreError
from graphstore.models import TraversalRequest
from graphstore.scheduler import make_run_id, plan_batches
from graphstore.snapshot import V2_SHARD_SCHEMA, v2_node_digest
from graphstore.util import canonical_json, digest_bytes


ROOT = Path(__file__).resolve().parents[1]


def _merge(store: GraphStore, sample_paths) -> dict:
    return store.merge(
        v1_path=sample_paths["v1"],
        v2_path=sample_paths["v2"],
        logical_clock_ms=sample_paths["logical_clock_ms"],
    )


def _request(**overrides) -> TraversalRequest:
    values = {
        "mode": "reach",
        "starts": ["n1", "n2", "n4", "n7"],
        "max_depth": 2,
        "max_parallel_tasks": 2,
        "memory_budget_entries": 1000,
        "batch_size": 1,
    }
    values.update(overrides)
    return TraversalRequest.create(**values)


def _add_incremental_shard(v2_root: Path) -> None:
    base = 1_700_000_070_000
    record = {
        "canonical_id": "n12",
        "label": "topic/checkpoint",
        "weight": 2.1,
        "write_ms": base,
        "tombstone": False,
        "out_edges": [
            {"target": "n4", "label": "mentions", "weight": 0.6}
        ],
    }
    record["node_digest"] = v2_node_digest(record)
    shard = {
        "schema": V2_SHARD_SCHEMA,
        "shard_id": "s0003",
        "generation": 3,
        "nodes": [record],
    }
    shard_path = v2_root / "shards" / "s0003.json"
    write_json(shard_path, shard)
    shard_hash = digest_bytes(shard_path.read_bytes())

    manifest_path = v2_root / "manifest.json"
    manifest = load_json(manifest_path)
    manifest["version"] = 3
    manifest["node_count_min"] = 7
    manifest["node_count_max"] = 7
    manifest["shards"].append(
        {
            "shard_id": "s0003",
            "file": "shards/s0003.json",
            "sha256": shard_hash,
            "generation": 3,
            "node_count": 1,
        }
    )
    write_json(manifest_path, manifest)

    connection = sqlite3.connect(v2_root / "metadata.sqlite")
    try:
        connection.execute(
            """
            UPDATE snapshot_meta
            SET version = 3, node_count_min = 7, node_count_max = 7
            WHERE id = 1
            """
        )
        connection.execute(
            """
            INSERT INTO shards(
                shard_id, file, sha256, generation, ordinal, node_count
            ) VALUES ('s0003', 'shards/s0003.json', ?, 3, 2, 1)
            """,
            (shard_hash,),
        )
        connection.execute(
            """
            INSERT INTO nodes(
                canonical_id, shard_id, shard_generation, write_ms,
                label, weight, tombstone, out_edges_json, node_digest
            ) VALUES ('n12', 's0003', 3, ?, 'topic/checkpoint', 2.1, 0, ?, ?)
            """,
            (
                record["write_ms"],
                canonical_json(record["out_edges"]),
                record["node_digest"],
            ),
        )
        connection.commit()
    finally:
        connection.close()


def test_invalid_snapshot_does_not_create_state(tmp_path, sample_paths):
    snapshot = load_json(sample_paths["v1"])
    snapshot["nodes"].append(dict(snapshot["nodes"][0]))
    write_json(sample_paths["v1"], snapshot)
    state_db = tmp_path / "state.sqlite"
    with pytest.raises(GraphStoreError) as caught:
        _merge(GraphStore(state_db), sample_paths)
    assert caught.value.code == ErrorCode.V1_DUPLICATE_NODE_ID
    assert not state_db.exists()


def test_merge_is_idempotent_and_snapshot_conflicts_are_rejected(
    tmp_path, sample_paths
):
    store = GraphStore(tmp_path / "state.sqlite")
    first = _merge(store, sample_paths)
    second = _merge(store, sample_paths)
    assert first["migration"]["decision"] == "INITIAL_LOAD"
    assert second["migration"]["decision"] == "IDEMPOTENT_REPLAY"
    assert first["edge_conflicts"] == [
        {
            "node_id": "n1",
            "target": "n2",
            "label": "follows",
            "winner_source": "v2:s0001",
            "winner_weight": 0.8,
            "sources": ["v1", "v2:s0001"],
            "candidates": first["edge_conflicts"][0]["candidates"],
        }
    ]
    decisions = store.decisions(first["migration"]["index_version"])
    assert any(
        item["kind"] == "edge"
        for item in decisions["decisions"]
    )

    with pytest.raises(GraphStoreError) as caught:
        store.merge(
            v1_path=sample_paths["v1"],
            v2_path=sample_paths["v2"],
            logical_clock_ms=sample_paths["logical_clock_ms"] + 1,
        )
    assert caught.value.code == ErrorCode.SNAPSHOT_CONFLICT

    snapshot = load_json(sample_paths["v1"])
    node = next(item for item in snapshot["nodes"] if item["id"] == "n9")
    node["weight"] = 9.9
    write_json(sample_paths["v1"], snapshot)
    with pytest.raises(GraphStoreError) as caught:
        _merge(store, sample_paths)
    assert caught.value.code == ErrorCode.SNAPSHOT_CONFLICT


def test_version_rollback_is_rejected(tmp_path, sample_paths):
    store = GraphStore(tmp_path / "state.sqlite")
    _merge(store, sample_paths)
    mutate_v2_manifest_and_metadata(sample_paths["v2"], {"version": 1})
    with pytest.raises(GraphStoreError) as caught:
        _merge(store, sample_paths)
    assert caught.value.code == ErrorCode.VERSION_ROLLBACK


def test_clock_rollback_threshold_is_enforced(tmp_path, sample_paths):
    store = GraphStore(tmp_path / "state.sqlite", clock_rollback_tolerance_ms=0)
    _merge(store, sample_paths)
    mutate_v2_manifest_and_metadata(
        sample_paths["v2"],
        {
            "version": 3,
            "logical_clock_floor": sample_paths["logical_clock_ms"] - 1,
        },
    )
    with pytest.raises(GraphStoreError) as caught:
        _merge(store, sample_paths)
    assert caught.value.code == ErrorCode.CLOCK_ROLLBACK_EXCEEDED


def test_full_replacement_switches_pointer_and_keeps_old_run_coherent(
    tmp_path, sample_paths
):
    store = GraphStore(tmp_path / "state.sqlite")
    _merge(store, sample_paths)
    old_run = store.traverse(_request())
    old_index_version = old_run["status"]["index"]["version"]
    mutate_v2_manifest_and_metadata(
        sample_paths["v2"],
        {"version": 3, "snapshot_id": "sample-v3-full"},
    )
    replacement = _merge(store, sample_paths)
    assert replacement["migration"]["decision"] == "FULL_REPLACE"
    current = store.status()
    assert current["index"]["version"] != old_index_version
    old_view = store.status(old_run["run_id"])
    assert old_view["index"]["version"] == old_index_version
    assert old_view["current_index_version"] == current["index"]["version"]


def test_incremental_append_detection(tmp_path, sample_paths):
    store = GraphStore(tmp_path / "state.sqlite")
    _merge(store, sample_paths)
    _add_incremental_shard(sample_paths["v2"])
    migration = _merge(store, sample_paths)
    assert migration["migration"]["decision"] == "INCREMENTAL_APPEND"
    assert migration["index"]["node_count"] == 12


def test_batch_plan_tamper_is_rejected_without_state_change(
    tmp_path, sample_paths
):
    store = GraphStore(tmp_path / "state.sqlite")
    _merge(store, sample_paths)
    index, _meta = store.state.load_index()
    request = _request()
    plans = plan_batches(index, request)
    run_id = make_run_id(index, request)
    store.state.ensure_run(
        run_id=run_id,
        index_version=index.index_version,
        request=request,
        plans=plans,
    )
    before = store.status(run_id)
    connection = sqlite3.connect(store.state.path)
    try:
        connection.execute(
            """
            UPDATE traversal_batches
            SET starts_json = ?
            WHERE run_id = ? AND batch_index = 0
            """,
            (canonical_json(["n7"]), run_id),
        )
        connection.commit()
    finally:
        connection.close()
    with pytest.raises(GraphStoreError) as caught:
        store.resume(run_id)
    assert caught.value.code == ErrorCode.CONTENT_DIGEST_MISMATCH
    after = store.status(run_id)
    assert after["traversal"]["outcomes"] == before["traversal"]["outcomes"]
    assert after["traversal"]["watermark"] == 0


def test_duplicate_batch_commit_is_idempotent(tmp_path, sample_paths):
    store = GraphStore(tmp_path / "state.sqlite")
    _merge(store, sample_paths)
    output = store.traverse(_request(max_parallel_tasks=1))
    batch = store.state.list_batches(output["run_id"])[0]
    result = batch["result"]
    first = store.state.commit_batch(
        run_id=output["run_id"],
        batch_key=batch["batch_key"],
        content_digest=batch["content_digest"],
        state="succeeded",
        result=result,
        reason=None,
    )
    assert first["idempotent"] is True
    with pytest.raises(GraphStoreError) as caught:
        store.state.commit_batch(
            run_id=output["run_id"],
            batch_key=batch["batch_key"],
            content_digest=batch["content_digest"],
            state="succeeded",
            result={"different": True},
            reason=None,
        )
    assert caught.value.code == ErrorCode.IDEMPOTENCY_CONFLICT


def _subprocess_env(delay_ms: int = 0) -> dict[str, str]:
    env = dict(os.environ)
    env["PYTHONPATH"] = str(ROOT)
    if delay_ms:
        env["GRAPHSTORE_TEST_BATCH_DELAY_MS"] = str(delay_ms)
    return env


def test_sigkill_recovery_replays_only_uncommitted_batches(
    tmp_path, sample_paths
):
    state_db = tmp_path / "state.sqlite"
    store = GraphStore(state_db)
    _merge(store, sample_paths)
    command = [
        sys.executable,
        "-m",
        "graphstore",
        "traverse",
        "--state-db",
        str(state_db),
        "--mode",
        "reach",
    ]
    for start in ("n1", "n2", "n4", "n7"):
        command.extend(["--start", start])
    command.extend(
        [
            "--max-depth",
            "2",
            "--max-parallel",
            "1",
            "--memory-budget",
            "1000",
            "--batch-size",
            "1",
        ]
    )
    process = subprocess.Popen(
        command,
        cwd=ROOT,
        env=_subprocess_env(500),
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        text=True,
    )
    run_id = None
    deadline = time.monotonic() + 15
    status = None
    while time.monotonic() < deadline:
        status = store.status()
        traversal = status.get("traversal")
        if traversal and traversal["watermark"] >= 1:
            run_id = traversal["run_id"]
            break
        time.sleep(0.02)
    assert run_id is not None, "traversal did not commit its first batch"
    os.kill(process.pid, signal.SIGKILL)
    process.wait(timeout=10)
    killed_status = store.status(run_id)
    assert killed_status["traversal"]["watermark"] == 1
    assert killed_status["traversal"]["outcomes"]["started_succeeded"] == 1

    resumed = subprocess.run(
        [
            sys.executable,
            "-m",
            "graphstore",
            "resume",
            "--state-db",
            str(state_db),
            "--run-id",
            run_id,
        ],
        cwd=ROOT,
        env=_subprocess_env(),
        capture_output=True,
        text=True,
        timeout=30,
        check=True,
    )
    resumed_payload = json.loads(resumed.stdout)
    assert resumed_payload["result"]["outcomes"] == {
        "abandoned": 0,
        "not_started": 0,
        "started_in_progress": 0,
        "started_succeeded": 4,
        "started_failed": 0,
    }
    final_digest = resumed_payload["result"]["batch_digests"]

    fresh_store = GraphStore(tmp_path / "fresh.sqlite")
    _merge(fresh_store, sample_paths)
    fresh = fresh_store.traverse(_request(max_parallel_tasks=1))
    assert fresh["result"]["batch_digests"] == final_digest

    connection = sqlite3.connect(state_db)
    try:
        commit_count = connection.execute(
            "SELECT COUNT(*) FROM batch_commit_log WHERE run_id = ?",
            (run_id,),
        ).fetchone()[0]
        duplicate_count = connection.execute(
            """
            SELECT COUNT(*) FROM (
                SELECT batch_key, COUNT(*) c
                FROM batch_commit_log
                WHERE run_id = ?
                GROUP BY batch_key
                HAVING c > 1
            )
            """,
            (run_id,),
        ).fetchone()[0]
    finally:
        connection.close()
    assert commit_count == 4
    assert duplicate_count == 0


def test_two_traversal_processes_commit_each_batch_once(
    tmp_path, sample_paths
):
    state_db = tmp_path / "state.sqlite"
    store = GraphStore(state_db)
    _merge(store, sample_paths)
    command = [
        sys.executable,
        "-m",
        "graphstore",
        "traverse",
        "--state-db",
        str(state_db),
        "--mode",
        "reach",
    ]
    for start in ("n1", "n2", "n4", "n7"):
        command.extend(["--start", start])
    command.extend(
        [
            "--max-parallel",
            "2",
            "--memory-budget",
            "1000",
            "--batch-size",
            "1",
        ]
    )
    processes = [
        subprocess.Popen(
            command,
            cwd=ROOT,
            env=_subprocess_env(100),
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            text=True,
        )
        for _ in range(2)
    ]
    for process in processes:
        _stdout, stderr = process.communicate(timeout=30)
        assert process.returncode == 0, stderr
    status = store.status()
    run_id = status["traversal"]["run_id"]
    assert status["traversal"]["watermark"] == 4
    assert status["traversal"]["outcomes"]["started_succeeded"] == 4
    connection = sqlite3.connect(state_db)
    try:
        rows = connection.execute(
            """
            SELECT batch_key, COUNT(*) c
            FROM batch_commit_log
            WHERE run_id = ?
            GROUP BY batch_key
            """,
            (run_id,),
        ).fetchall()
    finally:
        connection.close()
    assert len(rows) == 4
    assert all(count == 1 for _key, count in rows)


def test_concurrent_merge_processes_commit_snapshot_once(
    tmp_path, sample_paths
):
    state_db = tmp_path / "state.sqlite"
    command = [
        sys.executable,
        "-m",
        "graphstore",
        "merge",
        "--v1",
        str(sample_paths["v1"]),
        "--v2",
        str(sample_paths["v2"]),
        "--state-db",
        str(state_db),
    ]
    processes = [
        subprocess.Popen(
            command,
            cwd=ROOT,
            env=_subprocess_env(),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        for _ in range(2)
    ]
    payloads = []
    for process in processes:
        stdout, stderr = process.communicate(timeout=30)
        assert process.returncode == 0, stderr
        payloads.append(json.loads(stdout))
    decisions = sorted(
        payload["migration"]["decision"] for payload in payloads
    )
    assert decisions == ["IDEMPOTENT_REPLAY", "INITIAL_LOAD"]
    connection = sqlite3.connect(state_db)
    try:
        versions = connection.execute(
            "SELECT COUNT(*) FROM index_versions"
        ).fetchone()[0]
    finally:
        connection.close()
    assert versions == 1


def test_traversal_continues_on_old_index_during_replacement(
    tmp_path, sample_paths
):
    state_db = tmp_path / "state.sqlite"
    store = GraphStore(state_db)
    _merge(store, sample_paths)
    old_index_version = store.status()["index"]["version"]
    traversal_command = _traverse_command_for_test(state_db)
    process = subprocess.Popen(
        traversal_command,
        cwd=ROOT,
        env=_subprocess_env(250),
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        text=True,
    )
    old_run_id = _wait_for_commit(state_db)

    replacement_v2 = tmp_path / "v2-next"
    shutil.copytree(sample_paths["v2"], replacement_v2)
    mutate_v2_manifest_and_metadata(
        replacement_v2,
        {"version": 3, "snapshot_id": "sample-v3-concurrent"},
    )
    merge_command = [
        sys.executable,
        "-m",
        "graphstore",
        "merge",
        "--v1",
        str(sample_paths["v1"]),
        "--v2",
        str(replacement_v2),
        "--state-db",
        str(state_db),
    ]
    _stdout, stderr = process.communicate(timeout=30)
    assert process.returncode == 0, stderr
    merged = subprocess.run(
        merge_command,
        cwd=ROOT,
        env=_subprocess_env(),
        capture_output=True,
        text=True,
        timeout=30,
        check=True,
    )
    assert json.loads(merged.stdout)["migration"]["decision"] == "FULL_REPLACE"
    old_view = store.status(old_run_id)
    assert old_view["index"]["version"] == old_index_version
    assert old_view["traversal"]["watermark"] == 4
    assert old_view["current_index_version"] != old_index_version


def _traverse_command_for_test(state_db: Path) -> list[str]:
    command = [
        sys.executable,
        "-m",
        "graphstore",
        "traverse",
        "--state-db",
        str(state_db),
        "--mode",
        "reach",
    ]
    for start in ("n1", "n2", "n4", "n7"):
        command.extend(["--start", start])
    command.extend(
        [
            "--max-depth",
            "2",
            "--max-parallel",
            "1",
            "--memory-budget",
            "1000",
            "--batch-size",
            "1",
        ]
    )
    return command


def _wait_for_commit(state_db: Path) -> str:
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline:
        connection = sqlite3.connect(state_db, timeout=5)
        try:
            row = connection.execute(
                """
                SELECT run_id, committed_batches
                FROM traversal_runs
                ORDER BY created_at_ms DESC, run_id DESC
                LIMIT 1
                """
            ).fetchone()
        except sqlite3.OperationalError:
            row = None
        finally:
            connection.close()
        if row is not None and row[1] >= 1:
            return row[0]
        time.sleep(0.02)
    raise TimeoutError("traversal did not commit")

