"""分块持久化、中断恢复与并发加载资源上限测试."""

import json
import os

import pytest

from castdiag.errors import DiagError, E_BUDGET, E_DIGEST, E_INTERRUPT
from castdiag.loader import MemoryBudget, load_shards
from castdiag.persist import ChunkWriter, INTERRUPT_MARKER, recover, version_id_of
from castdiag.pipeline import InterruptFlag, run
from castdiag.validate import load_events, load_manifest


def _version(sample_dir):
    manifest = load_manifest(sample_dir / "sessions.json")
    events = load_events(sample_dir / "frames.jsonl")
    return version_id_of(manifest, events)


def test_chunk_write_and_recover(sample_dir, tmp_path):
    state = tmp_path / "state"
    report = run(sample_dir, state, chunk_count=4)
    assert report["persistence"]["chunks_written"] == 4
    bodies = recover(state, _version(sample_dir))
    assert len(bodies) == 4
    assert bodies[0]["merged_view"] is not None
    assert bodies[-1]["states"]["living-tv"]["position_ms"] == 2937


def test_recover_ignores_corrupted_tail(sample_dir, tmp_path):
    state = tmp_path / "state"
    run(sample_dir, state, chunk_count=4)
    tail = state / "chunk_00003.json"
    tail.write_bytes(b'{"format":"castdiag.chunk","broken":')  # 强制终止留下的截断文件
    bodies = recover(state, _version(sample_dir))
    assert len(bodies) == 3  # 仅采纳已持久化且摘要有效的前缀


def test_recover_rejects_digest_mismatch(sample_dir, tmp_path):
    state = tmp_path / "state"
    run(sample_dir, state, chunk_count=4)
    path = state / "chunk_00001.json"
    envelope = json.loads(path.read_text())
    envelope["body"]["counts"]["adopt"] = 999  # 篡改分块内容
    path.write_bytes(json.dumps(envelope).encode())
    bodies = recover(state, _version(sample_dir))
    assert len(bodies) == 1


def test_interrupt_only_at_chunk_boundary(sample_dir, tmp_path):
    state = tmp_path / "state"
    flag = InterruptFlag()
    flag.set("test:SIGTERM")
    report = run(sample_dir, state, chunk_count=4, interrupt=flag)
    assert report["persistence"]["interrupted"] is True
    assert report["persistence"]["chunks_written"] == 0
    marker = json.loads((state / INTERRUPT_MARKER).read_text())
    assert marker["code"] == E_INTERRUPT
    assert marker["next_chunk_index"] == 0


def test_loader_parallel_ok(sample_dir):
    snap = sample_dir / "snapshots"
    dig = json.loads((snap / "digests.json").read_text())
    v2_digests = {rel.split("/", 1)[1]: sha
                  for rel, sha in dig["generations"]["v2"]["files"].items()}
    shard_paths = sorted(str(p) for p in (snap / "v2").glob("shard_*.json"))
    budget = MemoryBudget(1024 * 1024)
    result = load_shards(shard_paths, v2_digests, max_workers=2, budget=budget)
    assert set(result) == {"shard_0.json", "shard_1.json"}
    assert result["shard_0.json"]["screens"] == ["bedroom-tv", "living-tv"]


def test_loader_first_error_cancels(sample_dir):
    snap = sample_dir / "snapshots"
    shard_paths = sorted(str(p) for p in (snap / "v2").glob("shard_*.json"))
    budget = MemoryBudget(1024 * 1024)
    with pytest.raises(DiagError) as exc:
        load_shards(shard_paths, {"shard_0.json": "0" * 64}, max_workers=2, budget=budget)
    assert exc.value.code == E_DIGEST


def test_loader_budget_exceeded(sample_dir):
    snap = sample_dir / "snapshots"
    shard_paths = sorted(str(p) for p in (snap / "v2").glob("shard_*.json"))
    with pytest.raises(DiagError) as exc:
        load_shards(shard_paths, {}, max_workers=1, budget=MemoryBudget(8))
    assert exc.value.code in (E_BUDGET, E_DIGEST)


def test_budget_waits_in_deterministic_order():
    budget = MemoryBudget(100)
    budget.acquire(100)
    budget.release(100)
    budget.acquire(50)
    budget.release(50)
    with pytest.raises(DiagError) as exc:
        budget.acquire(101)
    assert exc.value.code == E_BUDGET


def test_no_tmp_files_left(sample_dir, tmp_path):
    state = tmp_path / "state"
    run(sample_dir, state)
    leftovers = [n for n in os.listdir(state) if n.endswith(".tmp")]
    assert leftovers == []
