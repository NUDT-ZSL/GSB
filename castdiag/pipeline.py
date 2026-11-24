"""端到端编排: 校验 -> 摘要防篡改 -> 并发加载 -> 合并 -> 重放校验 -> 分块持久化."""

from __future__ import annotations

import json
import math
import signal
import threading
from pathlib import Path

from .canon import canonical
from .digest import select_valid_generations
from .loader import MemoryBudget, load_shards
from .merge import load_v1_records, load_v2_records, merge_records
from .persist import ChunkWriter, atomic_write, recover, version_id_of
from .replay import replay_events, sort_events
from .validate import load_events, load_manifest, validate_timeline
from .verify import build_initial_states, verify_replay

DEFAULT_BUDGET_BYTES = 64 * 1024 * 1024
DEFAULT_WORKERS = 4
DEFAULT_CHUNKS = 4


class InterruptFlag:
    """可处理终止信号: 仅在分块边界被检查并生效."""

    def __init__(self):
        self._event = threading.Event()
        self.reason = None

    def install(self) -> None:
        def _handler(signum, _frame):
            self.reason = f"signal:{signum}"
            self._event.set()

        for sig in (signal.SIGINT, signal.SIGTERM):
            try:
                signal.signal(sig, _handler)
            except (ValueError, OSError):
                pass  # 非主线程或平台不支持时跳过

    def set(self, reason: str = "manual") -> None:
        self.reason = reason
        self._event.set()

    def triggered(self) -> bool:
        return self._event.is_set()


def prepare(data_dir, workers: int = DEFAULT_WORKERS, budget_bytes: int = DEFAULT_BUDGET_BYTES):
    """校验输入、防篡改校验、并发加载分片并合并两代快照."""
    data_dir = Path(data_dir)
    manifest = load_manifest(data_dir / "sessions.json")
    events = load_events(data_dir / "frames.jsonl")
    snap_dir = data_dir / "snapshots"
    valid_gens, rejected = select_valid_generations(snap_dir)
    records = []
    fallback = None
    if "v2" in valid_gens:
        shard_dir = snap_dir / "v2"
        shard_paths = sorted(str(p) for p in shard_dir.glob("shard_*.json"))
        dig_manifest = json.loads((snap_dir / "digests.json").read_text(encoding="utf-8"))
        v2_digests = {
            rel.split("/", 1)[1]: sha
            for rel, sha in dig_manifest["generations"]["v2"]["files"].items()
        }
        budget = MemoryBudget(budget_bytes)
        shards = load_shards(shard_paths, v2_digests, max_workers=workers, budget=budget)
        records.extend(load_v2_records(snap_dir, shards))
    else:
        fallback = {"to": "v1", "reason": "v2 快照失效, 回退到最近一份有效快照"}
    if "v1" in valid_gens:
        v1_path = snap_dir / "v1" / "snapshot_v1.json"
        v1_raw = json.loads(v1_path.read_text(encoding="utf-8"))
        v2_rows = [r for r in records if r["source"].startswith("v2:")]
        validate_timeline(v1_raw, v2_rows)
        records.extend(load_v1_records(v1_path))
    elif fallback is None:
        fallback = {"to": "v2", "reason": "v1 快照失效, 仅使用 v2 有效快照"}
    view, conflicts = merge_records(records)
    version_id = version_id_of(manifest, events)
    return {
        "manifest": manifest,
        "events": events,
        "view": view,
        "conflicts": conflicts,
        "rejected_snapshots": rejected,
        "fallback": fallback,
        "version_id": version_id,
    }


def _chunk_boundaries(events: list[dict], chunk_count: int, budget_bytes: int) -> int:
    """按逻辑时刻分块: 块大小同时受配置块数与重放缓冲内存预算约束."""
    if not events:
        return 1
    avg = max(1, sum(len(canonical(e)) for e in events[:32]) // min(32, len(events)))
    by_budget = max(1, (budget_bytes // 2) // avg)
    by_count = max(1, math.ceil(len(events) / max(1, chunk_count)))
    return max(1, min(by_budget, by_count))


def run(data_dir, state_dir, workers: int = DEFAULT_WORKERS,
        budget_bytes: int = DEFAULT_BUDGET_BYTES, chunk_count: int = DEFAULT_CHUNKS,
        interrupt: InterruptFlag | None = None) -> dict:
    """完整诊断流程, 返回报告字典(同时原子写入 state_dir/report.json)."""
    prep = prepare(data_dir, workers=workers, budget_bytes=budget_bytes)
    events = sort_events(prep["events"])
    view = prep["view"]
    version_id = prep["version_id"]
    verify_report = verify_replay(view, events)
    writer = ChunkWriter(state_dir)
    flag = interrupt or InterruptFlag()
    per_chunk = _chunk_boundaries(events, chunk_count, budget_bytes)
    states = build_initial_states(view)
    total_counts = {"adopt": 0, "late_drop": 0, "rollback": 0}
    chunks_written = 0
    interrupted = False
    for index, start in enumerate(range(0, max(1, len(events)), per_chunk)):
        if flag.triggered():
            # 终止信号只在分块边界生效
            writer.write_interrupted(version_id, index, flag.reason or "unknown")
            interrupted = True
            break
        chunk_events = events[start:start + per_chunk]
        states, trace, counts = replay_events(chunk_events, states)
        for key in total_counts:
            total_counts[key] += counts[key]
        body = {
            "chunk_index": index,
            "event_seq_range": [chunk_events[0]["seq"], chunk_events[-1]["seq"]]
            if chunk_events else None,
            "merged_view": view if index == 0 else None,
            "trace": trace,
            "states": {sid: st.to_dict() for sid, st in sorted(states.items())},
            "counts": counts,
        }
        writer.write_chunk(index, version_id, body)
        chunks_written += 1
    recovered = recover(state_dir, version_id)
    report = {
        "ok": verify_report["ok"] and not interrupted,
        "version_id": version_id,
        "fallback": prep["fallback"],
        "rejected_snapshots": prep["rejected_snapshots"],
        "merge": {
            "screens": sorted(view.keys()),
            "conflicts": prep["conflicts"],
        },
        "verify": verify_report,
        "persistence": {
            "chunks_written": chunks_written,
            "chunks_recovered": len(recovered),
            "interrupted": interrupted,
            "state_dir": str(state_dir),
        },
        "adoption": total_counts,
    }
    atomic_write(Path(state_dir) / "report.json", report)
    return report
