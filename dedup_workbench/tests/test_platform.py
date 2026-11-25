from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from dedup_workbench.cache import ContentAddressedCache
from dedup_workbench.canonical import canonical_bytes
from dedup_workbench.engine import DedupWorkbench
from dedup_workbench.errors import WorkbenchError
from dedup_workbench.selfcheck import run_self_check
from dedup_workbench.snapshots import SnapshotStore
from dedup_workbench.testing import base_config, make_record, write_jsonl


def test_snapshot_versions_reject_rollback(tmp_path):
    store = SnapshotStore(tmp_path / "snapshots")
    first = store.commit("stats", {"value": 1})
    second = store.commit("stats", {"value": 2})
    assert first["version"] == 1
    assert second["version"] == 2

    with pytest.raises(WorkbenchError) as exc_info:
        store.install_version("stats", 1, {"value": 1})
    assert exc_info.value.code == "E_SNAPSHOT_ROLLBACK"


def test_snapshot_checksum_detects_tampering(tmp_path):
    store = SnapshotStore(tmp_path / "snapshots")
    store.commit("removals", {"ids": ["doc-1"]})
    path = store.path_for("removals")
    envelope = json.loads(path.read_text(encoding="utf-8"))
    envelope["data"]["ids"].append("doc-2")
    path.write_text(json.dumps(envelope), encoding="utf-8")

    with pytest.raises(WorkbenchError) as exc_info:
        store.load("removals")
    assert exc_info.value.code == "E_SNAPSHOT_CHECKSUM"


def test_concurrent_config_commit_gets_stage_busy_immediately(tmp_path):
    store = SnapshotStore(tmp_path / "snapshots")
    with store.config_commit_guard():
        with ThreadPoolExecutor(max_workers=1) as executor:
            future = executor.submit(store.commit, "config", {"config_version": 9})
            with pytest.raises(WorkbenchError) as exc_info:
                future.result(timeout=2)
    assert exc_info.value.code == "E_STAGE_BUSY"
    committed = store.commit("config", {"config_version": 10})
    assert committed["version"] == 1


def test_shard_resource_limit_has_structured_field(tmp_path):
    records = [
        make_record("doc-1", "first shard text", shard_id="shard-a"),
        make_record("doc-2", "second shard text", shard_id="shard-b"),
    ]
    input_path = write_jsonl(tmp_path / "corpus.jsonl", records)
    config = base_config(limits={"max_shards": 1})
    with pytest.raises(WorkbenchError) as exc_info:
        DedupWorkbench(tmp_path / "runtime", config).run(input_path)
    assert exc_info.value.code == "E_RESOURCE_LIMIT"
    assert exc_info.value.field == "limits.max_shards"


def test_block_resource_limit_has_doc_id(tmp_path):
    record = make_record("doc-blocks", "x" * 80)
    input_path = write_jsonl(tmp_path / "corpus.jsonl", [record])
    config = base_config(chunk_signature_length=4, limits={"max_blocks_per_doc": 3})
    with pytest.raises(WorkbenchError) as exc_info:
        DedupWorkbench(tmp_path / "runtime", config).run(input_path)
    assert exc_info.value.code == "E_RESOURCE_LIMIT"
    assert exc_info.value.field == "limits.max_blocks_per_doc"
    assert exc_info.value.details["doc_id"] == "doc-blocks"


def test_cache_memory_limit_can_raise(tmp_path):
    cache = ContentAddressedCache(tmp_path / "cas", max_memory_bytes=1024, overflow_policy="error")
    with pytest.raises(WorkbenchError) as exc_info:
        cache.get_or_compute("large", {"k": 1}, lambda: "x" * 5000)
    assert exc_info.value.code == "E_RESOURCE_LIMIT"


def test_cache_memory_priority_eviction(tmp_path):
    cache = ContentAddressedCache(tmp_path / "cas", max_memory_bytes=1000, overflow_policy="evict_then_spill")
    cache.get_or_compute("low", {"k": "low"}, lambda: "a" * 350, priority=1)
    cache.get_or_compute("high", {"k": "high"}, lambda: "b" * 450, priority=10)
    cache.get_or_compute("medium", {"k": "medium"}, lambda: "c" * 250, priority=5)
    assert cache.evictions
    assert cache.resident_bytes <= cache.max_memory_bytes
    assert cache.stats()["eviction_count"] >= 1


def test_self_check_raises_inconsistent_for_tampered_closure(tmp_path):
    records = [
        make_record("doc-1", "consistency closure text"),
        make_record("doc-2", "consistency closure text"),
    ]
    input_path = write_jsonl(tmp_path / "corpus.jsonl", records)
    report = DedupWorkbench(tmp_path / "runtime", base_config()).run(input_path)
    report["candidates"]["pair_count"] += 1
    with pytest.raises(WorkbenchError) as exc_info:
        run_self_check(report)
    assert exc_info.value.code == "E_INCONSISTENT"
    assert exc_info.value.details["failures"]


def test_canonical_export_is_valid_utf8(tmp_path):
    records = [make_record("doc-zh", "中文确定性去重文本，包含多个签名块。" * 3, lang="zh")]
    input_path = write_jsonl(tmp_path / "corpus.jsonl", records)
    workbench = DedupWorkbench(tmp_path / "runtime", base_config())
    workbench.run(input_path)
    raw = (tmp_path / "runtime" / "exports" / "doc_records.jsonl").read_bytes()
    assert raw.decode("utf-8").count("\n") == 1
    assert canonical_bytes({"ok": "中文"})
