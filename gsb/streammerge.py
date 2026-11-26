"""在可注入逻辑时钟下归并主库提交事件流与从库回放事件流。

归并键为 ``(逻辑时钟毫秒, 事件类型优先级, 事务号, 回放标识)``：
同时刻先提交后回放，其余同键情况用事务号/回放标识兜底，保证任意输入
顺序下输出都确定、稳定。
"""
from __future__ import annotations

from typing import Any, Dict, List

from .model import Bundle


def merge_site_streams(bundle: Bundle) -> List[Dict[str, Any]]:
    txn_by_id = bundle.txn_by_id()
    events: List[Dict[str, Any]] = []

    for txn in bundle.transactions:
        events.append({
            "kind": "master_commit",
            "logical_clock_ms": txn.commit_clock_ms,
            "txn_id": txn.txn_id,
            "segment_id": txn.segment_id,
            "bytes": txn.nbytes,
            "kind_priority": 0,
            "replay_id": "",
            "instance_id": "",
        })

    for rec in bundle.replica_records:
        txn = txn_by_id.get(rec.txn_id)
        events.append({
            "kind": "replica_replay",
            "logical_clock_ms": rec.replay_clock_ms,
            "txn_id": rec.txn_id,
            "segment_id": txn.segment_id if txn else None,
            "bytes": txn.nbytes if txn else None,
            "kind_priority": 1,
            "replay_id": rec.replay_id,
            "instance_id": rec.instance_id,
            "result": rec.result,
            "tombstone": rec.tombstone,
        })

    events.sort(key=lambda e: (
        e["logical_clock_ms"], e["kind_priority"], e["txn_id"], e["replay_id"]
    ))
    for idx, event in enumerate(events):
        event["order"] = idx
        event.pop("kind_priority", None)
    return events


def stream_summary(bundle: Bundle) -> Dict[str, Any]:
    events = merge_site_streams(bundle)
    return {
        "event_count": len(events),
        "master_commit_count": sum(1 for e in events if e["kind"] == "master_commit"),
        "replica_replay_count": sum(1 for e in events if e["kind"] == "replica_replay"),
        "first_clock_ms": events[0]["logical_clock_ms"] if events else None,
        "last_clock_ms": events[-1]["logical_clock_ms"] if events else None,
        "events": events,
    }
