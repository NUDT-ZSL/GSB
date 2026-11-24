from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any

from .errors import E_REFERENCE, E_SCHEMA, E_TIMELINE, ScreenError
from .loader import open_metadata_db
from .validate import load_json, relative_seconds_to_ms


def _find_canonical(raw_id: Any, manifest: dict[str, Any] | None) -> str:
    if not isinstance(raw_id, str) or not raw_id:
        raise ScreenError(E_SCHEMA, "快照记录缺少接收端标识", {"receiver_id": raw_id})
    if manifest:
        return manifest.get("alias_index", {}).get(raw_id, raw_id)
    return raw_id


def _session_start(session_id: str | None, manifest: dict[str, Any] | None, receiver_id: str) -> int:
    if not manifest:
        return 0
    if session_id and session_id in manifest.get("session_index", {}):
        return int(manifest["session_index"][session_id]["start_ms"])
    receiver = manifest.get("receiver_index", {}).get(receiver_id)
    if receiver:
        return int(manifest["session_index"][receiver["session_id"]]["start_ms"])
    raise ScreenError(E_REFERENCE, "快照接收端无法映射到会话起点", {"receiver_id": receiver_id, "session_id": session_id})


def load_v1_records(
    path: str | Path,
    manifest: dict[str, Any] | None = None,
    *,
    source_set_id: str = "v1",
    source_shard: str | None = None,
) -> list[dict[str, Any]]:
    path = Path(path)
    data = load_json(path, what="v1 快照")
    if not isinstance(data, dict):
        raise ScreenError(E_SCHEMA, "v1 快照根节点必须是对象", {"path": str(path)})
    raw_records = data.get("records", data.get("receivers", data.get("screens", {})))
    if isinstance(raw_records, list):
        record_items = []
        for item in raw_records:
            if not isinstance(item, dict):
                raise ScreenError(E_SCHEMA, "v1 记录必须是对象", {"path": str(path)})
            raw_id = item.get("receiver_id") or item.get("screen_id") or item.get("id")
            record_items.append((raw_id, item))
    elif isinstance(raw_records, dict):
        record_items = list(raw_records.items())
    else:
        raise ScreenError(E_SCHEMA, "v1 records/receivers 必须是对象或数组", {"path": str(path)})

    default_generation = int(data.get("generation", 1) or 1)
    relative_shard = source_shard or path.name
    records: list[dict[str, Any]] = []
    for raw_id, entry in record_items:
        if not isinstance(entry, dict):
            raise ScreenError(E_SCHEMA, "v1 接收端记录必须是对象", {"receiver_id": raw_id})
        canonical_id = _find_canonical(raw_id, manifest)
        session_id = entry.get("session_id")
        if manifest and canonical_id in manifest.get("receiver_index", {}):
            session_id = manifest["receiver_index"][canonical_id]["session_id"]
        start_ms = _session_start(session_id, manifest, canonical_id)
        relative_seconds = entry.get(
            "relative_play_seconds",
            entry.get("rel_play_seconds", entry.get("relative_position_s", entry.get("rel_start_s"))),
        )
        if relative_seconds is None:
            raise ScreenError(E_TIMELINE, "v1 记录缺少相对播放秒数", {"receiver_id": raw_id})
        absolute_ms = relative_seconds_to_ms(
            relative_seconds, start_ms, receiver_id=canonical_id, source=str(relative_shard)
        )
        cursor = entry.get(
            "flat_cursor_seq",
            entry.get("adoption_cursor_seq", entry.get("cursor_seq", entry.get("frame_no", 0))),
        )
        if not isinstance(cursor, int) or isinstance(cursor, bool) or cursor < 0:
            raise ScreenError(E_SCHEMA, "v1 扁平游标必须是非负整数", {"receiver_id": raw_id, "value": cursor})
        keyframe = entry.get("keyframe_seq", entry.get("keyframe_no"))
        record = {
            "canonical_receiver_id": canonical_id,
            "source_receiver_id": str(raw_id),
            "session_id": session_id,
            "generation": int(entry.get("generation", default_generation)),
            "logical_ts_ms": absolute_ms,
            "adoption_cursor_seq": int(cursor),
            "playback_position_ms": int(entry.get("playback_position_ms", absolute_ms)),
            "keyframe_seq": int(keyframe) if isinstance(keyframe, int) and not isinstance(keyframe, bool) else None,
            "tombstone": bool(entry.get("tombstone", data.get("tombstone", False))),
            "wait_gap": entry.get("wait_gap"),
            "source_set_id": source_set_id,
            "source_shard": relative_shard,
            "source_location": f"{relative_shard}#{raw_id}",
        }
        records.append(record)
    return records


def _table_columns(connection: sqlite3.Connection, table: str) -> set[str]:
    return {row[1] for row in connection.execute(f"PRAGMA table_info({table})").fetchall()}


def _choose_metadata_table(connection: sqlite3.Connection) -> str:
    names = {
        row[0]
        for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
    }
    for candidate in ("receiver_metadata", "receiver_state", "receivers", "screens"):
        if candidate in names:
            return candidate
    if not names:
        raise ScreenError(E_SCHEMA, "v2 SQLite 元数据库没有数据表")
    return sorted(names)[0]


def _row_value(row: sqlite3.Row, columns: set[str], keys: tuple[str, ...], default: Any = None) -> Any:
    for key in keys:
        if key in columns and row[key] is not None:
            return row[key]
    return default


def load_v2_records(
    snapshot_root: str | Path,
    set_entry: dict[str, Any],
    shards: dict[str, dict[str, Any]],
    manifest: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    root = Path(snapshot_root)
    db_entry = None
    for file_entry in set_entry.get("files", []):
        if file_entry.get("kind") == "sqlite" or str(file_entry.get("path", "")).endswith((".sqlite3", ".db")):
            db_entry = file_entry
            break
    if db_entry is None:
        raise ScreenError(E_SCHEMA, "v2 快照集合缺少 SQLite 元数据库", {"set_id": set_entry.get("set_id")})
    db_path = root / str(db_entry["path"])
    connection = open_metadata_db(db_path)
    try:
        table = _choose_metadata_table(connection)
        columns = _table_columns(connection, table)
        rows = connection.execute(f"SELECT * FROM {table}").fetchall()
    except sqlite3.Error as exc:
        raise ScreenError(E_SCHEMA, "读取 v2 SQLite 元数据失败", {"path": str(db_path), "cause": str(exc)}) from exc
    finally:
        connection.close()

    shard_record_index: dict[str, tuple[str, dict[str, Any]]] = {}
    for shard_path, payload in sorted(shards.items()):
        for item in payload.get("records", []):
            if not isinstance(item, dict):
                continue
            rid = str(item.get("canonical_receiver_id") or item.get("receiver_id"))
            shard_record_index[rid] = (shard_path, item)

    records: list[dict[str, Any]] = []
    db_ids: set[str] = set()
    for row in rows:
        raw_id = _row_value(row, columns, ("canonical_receiver_id", "receiver_id", "screen_id", "id"))
        canonical_id = _find_canonical(raw_id, manifest)
        db_ids.add(canonical_id)
        source_shard = _row_value(row, columns, ("source_shard", "shard_path", "shard"))
        shard_pair = shard_record_index.get(canonical_id)
        if source_shard is None and shard_pair:
            source_shard = shard_pair[0]
        if shard_pair is None:
            raise ScreenError(E_SCHEMA, "SQLite 记录缺少对应 v2 JSON 分片", {"receiver_id": canonical_id})
        shard_path, shard_record = shard_pair
        source_shard = str(source_shard or shard_path)

        ts_ms = _row_value(row, columns, ("logical_ts_ms", "ts_ms", "timestamp_ms", "absolute_ms"))
        cursor = _row_value(row, columns, ("adoption_cursor_seq", "cursor_seq", "frame_seq", "play_frame_no"), 0)
        position = _row_value(row, columns, ("playback_position_ms", "position_ms"), ts_ms)
        generation = _row_value(row, columns, ("generation", "session_generation"), set_entry.get("generation", 2))
        tombstone = _row_value(row, columns, ("tombstone", "tombstoned", "is_tombstone"), 0)
        gap_raw = _row_value(row, columns, ("gap_json", "wait_gap_json", "open_gap_json"))
        wait_gap = None
        if gap_raw:
            try:
                wait_gap = json.loads(gap_raw) if isinstance(gap_raw, str) else gap_raw
            except json.JSONDecodeError as exc:
                raise ScreenError(E_SCHEMA, "v2 gap_json 不可解析", {"receiver_id": canonical_id}) from exc
        gap_intervals_raw = _row_value(row, columns, ("gap_intervals_json", "gap_intervals"))
        gap_intervals = []
        if gap_intervals_raw:
            try:
                gap_intervals = (
                    json.loads(gap_intervals_raw)
                    if isinstance(gap_intervals_raw, str)
                    else gap_intervals_raw
                )
            except json.JSONDecodeError as exc:
                raise ScreenError(
                    E_SCHEMA, "v2 gap_intervals_json 不可解析", {"receiver_id": canonical_id}
                ) from exc

        record = {
            "canonical_receiver_id": canonical_id,
            "source_receiver_id": str(_row_value(row, columns, ("source_receiver_id",), raw_id)),
            "session_id": _row_value(row, columns, ("session_id",)),
            "generation": int(generation),
            "logical_ts_ms": int(ts_ms),
            "adoption_cursor_seq": int(cursor),
            "playback_position_ms": int(position),
            "keyframe_seq": _row_value(row, columns, ("keyframe_seq", "keyframe_no")),
            "tombstone": bool(tombstone),
            "wait_gap": wait_gap if isinstance(wait_gap, dict) else None,
            "gap_intervals": gap_intervals if isinstance(gap_intervals, list) else [],
            "source_set_id": str(set_entry.get("set_id", "v2")),
            "source_shard": source_shard,
            "source_location": f"{source_shard}#{canonical_id}",
        }
        if manifest and canonical_id in manifest.get("receiver_index", {}):
            record["session_id"] = manifest["receiver_index"][canonical_id]["session_id"]
        for field in ("generation", "logical_ts_ms", "adoption_cursor_seq", "playback_position_ms", "tombstone"):
            shard_value = shard_record.get(field)
            if shard_value is not None and shard_value != record[field]:
                raise ScreenError(
                    E_SCHEMA,
                    "v2 JSON 分片与 SQLite 元数据字段不一致",
                    {"receiver_id": canonical_id, "field": field, "sqlite": record[field], "shard": shard_value},
                )
        records.append(record)

    shard_ids = set(shard_record_index)
    if shard_ids != db_ids:
        raise ScreenError(
            E_SCHEMA,
            "v2 分片记录集合与 SQLite 元数据不一致",
            {"shard_only": sorted(shard_ids - db_ids), "sqlite_only": sorted(db_ids - shard_ids)},
        )
    return records


def _source_brief(record: dict[str, Any], value: Any) -> dict[str, Any]:
    return {
        "source_set_id": record["source_set_id"],
        "source_shard": record["source_shard"],
        "source_location": record["source_location"],
        "source_receiver_id": record.get("source_receiver_id"),
        "generation": record["generation"],
        "logical_ts_ms": record["logical_ts_ms"],
        "tombstone": record["tombstone"],
        "value": value,
    }


def _winner_rule(winner: dict[str, Any], loser: dict[str, Any]) -> str:
    if winner["tombstone"] and not loser["tombstone"]:
        return "tombstone_priority"
    if not winner["tombstone"] and loser["tombstone"]:
        return "deterministic_source_tie_break"
    if winner["generation"] != loser["generation"]:
        return "newer_generation"
    if winner["logical_ts_ms"] != loser["logical_ts_ms"]:
        return "later_logical_ms"
    return "deterministic_source_tie_break"


def merge_records(records: list[dict[str, Any]]) -> tuple[dict[str, dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
    by_receiver: dict[str, list[dict[str, Any]]] = {}
    for record in records:
        by_receiver.setdefault(record["canonical_receiver_id"], []).append(record)

    view: dict[str, dict[str, Any]] = {}
    conflicts: list[dict[str, Any]] = []
    adjudications: list[dict[str, Any]] = []

    for receiver_id in sorted(by_receiver):
        group = sorted(
            by_receiver[receiver_id],
            key=lambda item: (
                1 if item["tombstone"] else 0,
                item["generation"],
                item["logical_ts_ms"],
                1 if str(item["source_set_id"]).startswith("v2") else 0,
                str(item["source_location"]),
            ),
            reverse=True,
        )
        winner = group[0]
        view[receiver_id] = {
            **winner,
            "all_sources": [item["source_location"] for item in group],
            "conflict_count": 0,
        }
        for loser in group[1:]:
            rule = _winner_rule(winner, loser)
            adjudications.append(
                {
                    "receiver_id": receiver_id,
                    "winner_source": winner["source_location"],
                    "loser_source": loser["source_location"],
                    "rule": rule,
                    "basis": "墓碑优先；非墓碑记录按会话代数、绝对逻辑毫秒裁决；同值时按 v2/来源位置稳定破平",
                }
            )
            for field in ("adoption_cursor_seq", "playback_position_ms"):
                if winner.get(field) != loser.get(field):
                    conflict = {
                        "receiver_id": receiver_id,
                        "field": field,
                        "rule": rule,
                        "winner": _source_brief(winner, winner.get(field)),
                        "loser": _source_brief(loser, loser.get(field)),
                    }
                    conflicts.append(conflict)
                    view[receiver_id]["conflict_count"] += 1
            if winner.get("generation") != loser.get("generation"):
                conflicts.append(
                    {
                        "receiver_id": receiver_id,
                        "field": "generation",
                        "rule": rule,
                        "winner": _source_brief(winner, winner.get("generation")),
                        "loser": _source_brief(loser, loser.get("generation")),
                    }
                )

    conflicts.sort(key=lambda item: (item["receiver_id"], item["field"], item["loser"]["source_location"]))
    adjudications.sort(key=lambda item: (item["receiver_id"], item["loser_source"]))
    return view, conflicts, adjudications
