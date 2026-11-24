"""两代会话快照合并为统一状态视图.

裁决规则(按优先级):
  1. 会话代数取新;
  2. 同代时墓碑标记优先于无墓碑记录;
  3. 其余按逻辑时刻取新。
两代对同一接收屏的关键帧序号或播放位置冲突时, 逐条产出可定位来源的冲突项。
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from .errors import DiagError, E_SCHEMA
from .validate import v1_rel_to_abs_ms


def load_v1_records(path) -> list[dict]:
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise DiagError(E_SCHEMA, "v1 快照不可解析", {"path": str(path), "cause": str(exc)})
    if data.get("format") != "castdiag.v1":
        raise DiagError(E_SCHEMA, "v1 快照格式标识非法", {"path": str(path)})
    base = data.get("base_epoch_ms")
    records = []
    for screen_id, entry in sorted(data.get("screens", {}).items()):
        records.append(
            {
                "screen_id": screen_id,
                "generation": int(data.get("generation", 1)),
                "ts_ms": v1_rel_to_abs_ms(base, entry.get("rel_start_s"), screen_id),
                "keyframe_seq": int(entry.get("keyframe_no", 0)),
                "play_frame_no": int(entry.get("frame_no", 0)),
                "tolerance_ms": None,
                "tombstone": False,
                "flags": list(entry.get("flags", [])),
                "source": "v1:snapshot_v1.json",
            }
        )
    return records


def load_v2_records(snapshot_dir, shards: dict) -> list[dict]:
    """从 SQLite 元数据库读取记录, 并用分片加载结果标注来源分片."""
    shard_of = {}
    for shard_name, payload in sorted(shards.items()):
        if payload.get("format") != "castdiag.v2.shard":
            raise DiagError(E_SCHEMA, "v2 分片格式标识非法", {"shard": shard_name})
        for screen_id in payload.get("screens", []):
            shard_of[screen_id] = shard_name
    db_path = Path(snapshot_dir) / "v2" / "meta.db"
    conn = sqlite3.connect(db_path)
    try:
        rows = conn.execute(
            "SELECT screen_id, ts_ms, generation, keyframe_seq, play_frame_no,"
            " tolerance_ms, tombstone FROM screens ORDER BY screen_id"
        ).fetchall()
    finally:
        conn.close()
    records = []
    for screen_id, ts_ms, generation, keyframe_seq, play_frame_no, tolerance_ms, tombstone in rows:
        shard = shard_of.get(screen_id)
        if shard is None:
            raise DiagError(E_SCHEMA, "元数据库记录无对应分片声明",
                            {"screen_id": screen_id})
        records.append(
            {
                "screen_id": screen_id,
                "generation": int(generation),
                "ts_ms": int(ts_ms),
                "keyframe_seq": int(keyframe_seq),
                "play_frame_no": int(play_frame_no),
                "tolerance_ms": int(tolerance_ms),
                "tombstone": bool(tombstone),
                "flags": [],
                "source": f"v2:meta.db#{screen_id}",
                "shard": f"v2/{shard}",
            }
        )
    declared = set(shard_of)
    actual = {r["screen_id"] for r in records}
    if declared != actual:
        raise DiagError(E_SCHEMA, "分片声明与元数据库接收屏集合不一致",
                        {"shard_only": sorted(declared - actual),
                         "db_only": sorted(actual - declared)})
    return records


def _rank(record: dict):
    return (record["generation"], record["tombstone"], record["ts_ms"])


def merge_records(records: list[dict]):
    """合并记录, 返回 (统一视图, 冲突项列表)."""
    by_screen: dict[str, list[dict]] = {}
    for rec in records:
        by_screen.setdefault(rec["screen_id"], []).append(rec)
    view: dict[str, dict] = {}
    conflicts: list[dict] = []
    for screen_id in sorted(by_screen):
        group = by_screen[screen_id]
        winner = max(group, key=_rank)
        view[screen_id] = winner
        for loser in group:
            if loser is winner:
                continue
            for field in ("keyframe_seq", "play_frame_no"):
                if loser[field] != winner[field]:
                    conflicts.append(
                        {
                            "screen_id": screen_id,
                            "field": field,
                            "rule": "generation>tombstone>ts_ms",
                            "kept": {
                                "source": winner["source"],
                                "generation": winner["generation"],
                                "ts_ms": winner["ts_ms"],
                                "value": winner[field],
                            },
                            "dropped": {
                                "source": loser["source"],
                                "generation": loser["generation"],
                                "ts_ms": loser["ts_ms"],
                                "value": loser[field],
                            },
                        }
                    )
    conflicts.sort(key=lambda c: (c["screen_id"], c["field"], c["dropped"]["source"]))
    return view, conflicts
